#!/usr/bin/env python3
"""Qwen 2.5 + MAX: promote Hermes-style tool text to OpenAI tool_calls.

When Qwen2.5 is served via local MAX, tool invocations often appear as plain
text, e.g.::

    <tool_call>{"name": "file_read", "arguments": {...}}</tool_call>

or a bare ``task_done`` line. OCR only acts on structured ``tool_calls`` in the
API response. This proxy:

1. Rewrites outbound chat requests (tool-use policy + few-shot; stronger
   empty-tool nudge than OCR's default).
2. Forwards to MAX and rewrites non-streaming responses when ``tool_calls``
   is empty but the content looks like a Hermes/JSON tool call, or when the
   model dumps a review essay (promoted to ``code_comment``).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE
)
MARKDOWN_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE
)
CURRENT_FILE_PATH_RE = re.compile(
    r"<current_file_path>\s*(.*?)\s*</current_file_path>",
    re.DOTALL | re.IGNORECASE,
)
# Weak models often copy OCR prompt tags literally into tool args.
PATH_PLACEHOLDERS = frozenset(
    {
        "<current_file_path>",
        "</current_file_path>",
        "<current_file_path></current_file_path>",
        "current_file_path",
        "<current_file_path/>",
    }
)
PATH_ARG_KEYS = frozenset({"file_path", "path", "filepath", "file"})

# Hallucinated / alternate names → OCR tools.
TOOL_NAME_ALIASES = {
    "code_review_current_file": "file_read",
    "code_review": "file_read",
    "review_file": "file_read",
    "review_current_file": "file_read",
    "read_file": "file_read",
    "read_current_file": "file_read",
    "submit_comment": "code_comment",
    "add_comment": "code_comment",
    "post_comment": "code_comment",
    "done": "task_done",
    "finish": "task_done",
    "complete": "task_done",
}

DIFF_PLACEHOLDERS = frozenset(
    {
        "<current_file_diff>",
        "</current_file_diff>",
        "<current_file_diff></current_file_diff>",
        "current_file_diff",
        "<current_file_diff/>",
    }
)

TASK_DONE_LINE_RE = re.compile(
    # /task_done, (task_done), [task_done], task_done, Task Done, …
    r"^[\[\(\{\s/\\]*task[\s_-]*done[\]\)\}\s/\\]*$",
    re.IGNORECASE,
)

# Weak models write markdown review essays instead of calling code_comment.
REVIEW_PROSE_MARKERS = (
    "potential issues",
    "security concern",
    "suggested fix",
    "line-by-line",
    "### analysis",
    "## analysis",
    "hardcoded",
    "api_key",
    "secret key",
    "command injection",
)
MIN_REVIEW_PROSE_CHARS = 350
MAX_PROMOTED_COMMENT_CHARS = 3500

# Injected once per conversation; marker prevents double-injection on retries.
TOOL_POLICY_MARKER = "<!-- qwen25-max-tool-policy -->"
OCR_EMPTY_TOOL_NUDGE = (
    "You did not successfully call any tools. "
    "Please try again or use task_done if finished."
)
STRONG_EMPTY_TOOL_NUDGE = (
    "You did not call any tools. Do NOT write a markdown analysis or essay. "
    "If you found issues in the diff, call code_comment now with path set to the "
    "current file and comments[].existing_code copied from the diff. "
    "To finish, emit exactly: "
    '<tool_call>{"name":"task_done","arguments":{"state":"DONE"}}</tool_call> '
    "(never /task_done, never (task_done), never prose). "
    "Call task_done ONLY if there are truly no issues. Prose reviews are invalid."
)

UPSTREAM = "http://127.0.0.1:8000"
LOG_PREFIX = "[qwen25-max-tool-call-proxy]"
PROXY_HEALTH_PATH = "/_qwen25_max_tool_call_proxy/health"

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _strip_markdown_fences(content: str) -> str:
    stripped = content.strip()
    match = MARKDOWN_FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _iter_balanced_json_objects(text: str) -> list[str]:
    """Yield top-level {...} substrings using brace matching."""
    objs: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    objs.append(text[i : j + 1])
                    i = j + 1
                    break
        else:
            i += 1
    return objs


def _parse_tool_call_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str) and name:
        return data
    fn = data.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str) and fn.get("name"):
        return {
            "name": fn["name"],
            "arguments": fn.get("arguments", {}),
        }
    return None


def _normalize_tool_name(name: str) -> str:
    cleaned = name.strip().lstrip("/").strip()
    lower = cleaned.lower().replace("-", "_").replace(" ", "_")
    if TASK_DONE_LINE_RE.match(cleaned) or lower in {"task_done", "taskdone"}:
        return "task_done"
    return TOOL_NAME_ALIASES.get(lower, cleaned)


def _normalize_tool_args(name: str, args: Any) -> Any:
    if name == "task_done":
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return {"state": "DONE"}
        if not isinstance(args, dict):
            return {"state": "DONE"}
        if not args:
            return {"state": "DONE"}
        if "state" not in args:
            return {**args, "state": "DONE"}
        return args

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args
    if not isinstance(args, dict):
        return args

    out = dict(args)
    # file_read expects file_path; models often pass path=.
    if name == "file_read" and "file_path" not in out and isinstance(out.get("path"), str):
        out["file_path"] = out.pop("path")
    # Drop OCR prompt-tag placeholders that are not real diffs/paths.
    for key in list(out.keys()):
        val = out[key]
        if isinstance(val, str) and val.strip() in DIFF_PLACEHOLDERS:
            del out[key]
    return out


def _make_tool_call(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("tool call missing name")
    name = _normalize_tool_name(name)
    args = data.get("arguments", data.get("parameters", {}))
    args = _normalize_tool_args(name, args)
    if isinstance(args, str):
        arg_str = args
    else:
        arg_str = json.dumps(args, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": arg_str},
    }


def _append_tool_call(
    calls: list[dict[str, Any]], seen: set[tuple[str, str]], data: dict[str, Any]
) -> None:
    try:
        call = _make_tool_call(data)
    except ValueError:
        return
    key = (call["function"]["name"], call["function"]["arguments"])
    if key in seen:
        return
    seen.add(key)
    calls.append(call)


def extract_tool_calls(content: str) -> list[dict[str, Any]]:
    if not content:
        return []

    calls: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for match in TOOL_CALL_BLOCK_RE.finditer(content):
        block = _strip_markdown_fences(match.group(1).strip())
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        payload = _parse_tool_call_payload(data)
        if payload:
            _append_tool_call(calls, seen, payload)

    if not calls:
        normalized = _strip_markdown_fences(content)
        candidates = [normalized, * _iter_balanced_json_objects(normalized)]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            payload = _parse_tool_call_payload(data)
            if payload:
                _append_tool_call(calls, seen, payload)

    stripped = _strip_markdown_fences(content).strip()
    if not calls and TASK_DONE_LINE_RE.match(stripped):
        _append_tool_call(calls, seen, {"name": "task_done", "arguments": {"state": "DONE"}})

    return calls


def extract_current_file_path(req_payload: dict[str, Any] | None) -> str | None:
    """Pull the real path from OCR's <current_file_path>…</current_file_path> tags."""
    if not req_payload:
        return None
    messages = req_payload.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        match = CURRENT_FILE_PATH_RE.search(content)
        if match:
            path = match.group(1).strip()
            if path and path not in PATH_PLACEHOLDERS:
                return path
    return None


def _looks_like_path_placeholder(value: str) -> bool:
    stripped = value.strip()
    if stripped in PATH_PLACEHOLDERS:
        return True
    # Model copied the opening tag name as the path.
    if stripped in {"<current_file_path>", "current_file_path"}:
        return True
    # Empty tag or tag-only string.
    match = CURRENT_FILE_PATH_RE.fullmatch(stripped)
    if match is not None and not match.group(1).strip():
        return True
    return False


def _normalize_repo_path(value: str, current_path: str | None) -> str:
    """OCR expects repo-relative paths; models often emit /scripts/foo.sh."""
    stripped = value.strip()
    if _looks_like_path_placeholder(stripped) and current_path:
        return current_path

    # Drop a single leading slash (not UNC //) and ./ prefixes.
    if stripped.startswith("/") and not stripped.startswith("//"):
        stripped = stripped[1:]
    while stripped.startswith("./"):
        stripped = stripped[2:]

    if current_path:
        cur = current_path.lstrip("/").lstrip("./")
        if stripped == cur or stripped.endswith("/" + cur):
            return current_path

    return stripped


def _rewrite_args_placeholders(
    args: Any, current_path: str | None
) -> tuple[Any, bool]:
    """Normalize path args, replace placeholders, drop JSON nulls."""
    changed = False
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return args, False
        rewritten, changed = _rewrite_args_placeholders(parsed, current_path)
        if changed:
            return json.dumps(rewritten, ensure_ascii=False), True
        return args, False
    if not isinstance(args, dict):
        return args, False

    out: dict[str, Any] = {}
    for key, value in args.items():
        if value is None:
            changed = True
            continue
        if key in PATH_ARG_KEYS and isinstance(value, str):
            normalized = _normalize_repo_path(value, current_path)
            if normalized != value:
                changed = True
            out[key] = normalized
            continue
        out[key] = value
    return out, changed


def rewrite_tool_calls(
    tool_calls: list[Any], current_path: str | None
) -> tuple[list[Any], bool]:
    if not tool_calls:
        return tool_calls, False
    changed_any = False
    out: list[Any] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            out.append(tc)
            continue
        name = fn.get("name")
        new_name = _normalize_tool_name(name) if isinstance(name, str) else name
        args = fn.get("arguments", {})
        new_args, args_changed = _rewrite_args_placeholders(args, current_path)
        # Also normalize args for aliased tools (path→file_path, drop diff tags).
        if isinstance(new_name, str):
            if isinstance(new_args, str):
                try:
                    parsed_args = json.loads(new_args)
                except json.JSONDecodeError:
                    parsed_args = new_args
            else:
                parsed_args = new_args
            normalized = _normalize_tool_args(new_name, parsed_args)
            if normalized != parsed_args:
                args_changed = True
                new_args = normalized
        name_changed = isinstance(name, str) and new_name != name
        if not args_changed and not name_changed:
            out.append(tc)
            continue
        changed_any = True
        new_fn = dict(fn)
        if name_changed:
            new_fn["name"] = new_name
        if args_changed:
            if isinstance(new_args, str):
                new_fn["arguments"] = new_args
            else:
                new_fn["arguments"] = json.dumps(new_args, ensure_ascii=False)
        new_tc = dict(tc)
        new_tc["function"] = new_fn
        out.append(new_tc)
    return out, changed_any


def _is_task_done_text(content: str) -> bool:
    return bool(TASK_DONE_LINE_RE.match(_strip_markdown_fences(content).strip()))


def _clean_promoted_content(content: str) -> str | None:
    """Remove tool-call text left in content after promotion."""
    cleaned = TOOL_CALL_BLOCK_RE.sub("", content)
    # Dangling open/close tags from truncated model output.
    cleaned = re.sub(r"</?tool_call>", "", cleaned, flags=re.IGNORECASE)
    cleaned = _strip_markdown_fences(cleaned.strip())
    if _is_task_done_text(cleaned) or cleaned == "":
        return None
    # Whole (or leading) JSON tool object — drop it once promoted.
    for candidate in _iter_balanced_json_objects(cleaned):
        try:
            if _parse_tool_call_payload(json.loads(candidate)):
                cleaned = cleaned.replace(candidate, "", 1).strip()
        except json.JSONDecodeError:
            continue
    cleaned = cleaned.strip()
    if _is_task_done_text(cleaned) or cleaned == "":
        return None
    try:
        if _parse_tool_call_payload(json.loads(_strip_markdown_fences(cleaned))):
            return None
    except json.JSONDecodeError:
        pass
    return cleaned or None


def looks_like_review_prose(content: str) -> bool:
    """True when the model wrote a review essay instead of calling tools."""
    stripped = content.strip()
    if len(stripped) < MIN_REVIEW_PROSE_CHARS:
        return False
    # Already a tool payload — leave to extract_tool_calls.
    if stripped.startswith("{") and '"name"' in stripped[:200]:
        return False
    if "<tool_call>" in stripped.lower():
        return False
    lower = stripped.lower()
    return any(marker in lower for marker in REVIEW_PROSE_MARKERS)


def _existing_code_hint(content: str) -> str | None:
    """Best-effort snippet for OCR line resolution from prose/code fences."""
    code_needles = (
        "API_KEY=",
        "while (( SECONDS",
        'eval "curl',
        "eval 'curl",
        "curl -k",
    )

    def pick_from(text: str) -> str | None:
        for line in text.splitlines():
            s = line.strip().lstrip("+").strip()
            s = re.sub(r"^[*`-]+\s*", "", s).strip("`")
            # Skip markdown prose that happens to mention eval/curl.
            if (
                not s
                or s.startswith("#")
                or s.startswith("**")
                or re.match(r"^\d+\.\s", s)
            ):
                continue
            if any(needle in s for needle in code_needles):
                return s[:200]
        return None

    for fence in re.finditer(
        r"```(?:bash|sh|shell|diff)?\s*\n(.*?)```", content, re.DOTALL
    ):
        hit = pick_from(fence.group(1))
        if hit:
            return hit
    return pick_from(content)


def prose_to_code_comment(
    content: str, current_path: str | None
) -> dict[str, Any] | None:
    """Wrap a review essay as a structured code_comment tool call for OCR."""
    if not current_path or not looks_like_review_prose(content):
        return None
    body = content.strip()
    if len(body) > MAX_PROMOTED_COMMENT_CHARS:
        body = body[: MAX_PROMOTED_COMMENT_CHARS - 20].rstrip() + "\n…(truncated)"
    comment: dict[str, Any] = {
        "content": (
            "[Promoted from model prose — call `code_comment` next time.]\n\n" + body
        ),
        "category": "security"
        if any(m in body.lower() for m in ("api_key", "secret", "hardcoded"))
        else "maintainability",
        "severity": "high"
        if any(m in body.lower() for m in ("api_key", "secret", "hardcoded"))
        else "medium",
    }
    hint = _existing_code_hint(content)
    if hint:
        comment["existing_code"] = hint
    return {
        "name": "code_comment",
        "arguments": {"path": current_path, "comments": [comment]},
    }


def build_tool_policy_text(current_path: str | None) -> str:
    path = current_path or "path/to/file.ext"
    # Keep this short: every token slows CPU decode and risks OCR's HTTP deadline.
    comment_example = (
        f'<tool_call>{{"name":"code_comment","arguments":{{"path":"{path}",'
        f'"comments":[{{"content":"Hardcoded secret.","existing_code":"API_KEY=...",'
        f'"category":"security","severity":"high"}}]}}}}</tool_call>'
    )
    done_example = (
        '<tool_call>{"name":"task_done","arguments":{"state":"DONE"}}</tool_call>'
    )
    return (
        f"{TOOL_POLICY_MARKER}\n"
        "## Tool-calling policy (mandatory)\n"
        "- Allowed tools ONLY: file_read, file_read_diff, code_comment, task_done, "
        "file_find, code_search. Never invent tools (e.g. code_review_current_file).\n"
        "- No markdown essays. Report issues only via code_comment.\n"
        f"- Paths are repo-relative (`{path}`), never `/{path}` or `<current_file_path>`.\n"
        "- Finish ONLY with this exact tool call (never prose, never /task_done, "
        "never (task_done)): "
        f"{done_example}\n"
        f"- Call task_done only after code_comment, or if there are truly no issues.\n"
        f"- Example code_comment: {comment_example}\n"
    )


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _set_message_text(msg: dict[str, Any], text: str) -> dict[str, Any]:
    out = dict(msg)
    content = msg.get("content")
    if isinstance(content, list):
        out["content"] = [{"type": "text", "text": text}]
    else:
        out["content"] = text
    return out


def _conversation_has_policy(messages: list[Any]) -> bool:
    for msg in messages:
        if isinstance(msg, dict) and TOOL_POLICY_MARKER in _message_text(msg):
            return True
    return False


def _last_assistant_was_prose(messages: list[Any]) -> bool:
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        if msg.get("tool_calls"):
            return False
        return looks_like_review_prose(_message_text(msg))
    return False


def rewrite_chat_request(req_payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Inject tool policy + rewrite OCR's weak empty-tool nudge."""
    messages = req_payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return req_payload, False

    changed = False
    current_path = extract_current_file_path(req_payload)
    new_messages: list[Any] = list(messages)

    if not _conversation_has_policy(new_messages):
        policy = build_tool_policy_text(current_path)
        inserted = False
        for i, msg in enumerate(new_messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                existing = _message_text(msg)
                new_messages[i] = _set_message_text(
                    msg, existing.rstrip() + "\n\n" + policy
                )
                inserted = True
                break
        if not inserted:
            new_messages.insert(0, {"role": "system", "content": policy})
        changed = True

    prose_escape = _last_assistant_was_prose(new_messages)
    for i, msg in enumerate(new_messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _message_text(msg)
        if OCR_EMPTY_TOOL_NUDGE not in text:
            continue
        if STRONG_EMPTY_TOOL_NUDGE in text:
            continue
        replaced = text.replace(OCR_EMPTY_TOOL_NUDGE, STRONG_EMPTY_TOOL_NUDGE)
        if prose_escape:
            replaced += (
                " Your previous message was a prose review; convert each "
                "finding into code_comment tool calls now."
            )
        new_messages[i] = _set_message_text(msg, replaced)
        changed = True

    if not changed:
        return req_payload, False
    out = dict(req_payload)
    out["messages"] = new_messages
    return out, True


def _maybe_rewrite_request_body(method: str, path: str, body: bytes) -> bytes:
    if method != "POST" or not path.rstrip("/").endswith("/chat/completions") or not body:
        return body
    try:
        req_payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(req_payload, dict) or req_payload.get("stream"):
        return body
    rewritten, changed = rewrite_chat_request(req_payload)
    if not changed:
        return body
    sys.stderr.write(f"{LOG_PREFIX} rewrote chat request (tool policy / nudge)\n")
    return json.dumps(rewritten, ensure_ascii=False).encode("utf-8")


def promote_message(
    message: dict[str, Any], current_path: str | None = None
) -> dict[str, Any]:
    existing = message.get("tool_calls")
    if existing:
        rewritten, changed = rewrite_tool_calls(
            existing if isinstance(existing, list) else [], current_path
        )
        content = message.get("content")
        content_changed = False
        new_content = content
        if isinstance(content, str) and content.strip():
            # Model often emits both structured tool_calls AND leftover text
            # like `{"name":...}\n<tool_call>`.
            new_content = _clean_promoted_content(content)
            content_changed = new_content != content
        if not changed and not content_changed:
            return message
        promoted = dict(message)
        if changed:
            promoted["tool_calls"] = rewritten
        if content_changed:
            promoted["content"] = new_content
        return promoted

    content = message.get("content")
    if not isinstance(content, str) or not content:
        return message
    tool_calls = extract_tool_calls(content)
    if not tool_calls:
        # Qwen2.5-1.5B often dumps a markdown review instead of code_comment.
        prose_call = prose_to_code_comment(content, current_path)
        if prose_call is None:
            return message
        tool_calls = []
        seen: set[tuple[str, str]] = set()
        _append_tool_call(tool_calls, seen, prose_call)
        # End the OCR loop; otherwise the model emits "(task_done)" text forever.
        _append_tool_call(
            tool_calls, seen, {"name": "task_done", "arguments": {"state": "DONE"}}
        )
        promoted = dict(message)
        promoted["tool_calls"] = tool_calls
        promoted["content"] = None
        return promoted
    tool_calls, _ = rewrite_tool_calls(tool_calls, current_path)
    promoted = dict(message)
    promoted["tool_calls"] = tool_calls
    promoted["content"] = _clean_promoted_content(content)
    return promoted


def promote_chat_completion(
    payload: dict[str, Any], req_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload
    current_path = extract_current_file_path(req_payload)
    out = dict(payload)
    new_choices = []
    changed_any = False
    for choice in choices:
        if not isinstance(choice, dict):
            new_choices.append(choice)
            continue
        msg = choice.get("message")
        if not isinstance(msg, dict):
            new_choices.append(choice)
            continue
        promoted = promote_message(msg, current_path)
        if promoted is msg:
            new_choices.append(choice)
        else:
            changed_any = True
            new_choice = dict(choice)
            new_choice["message"] = promoted
            if promoted.get("tool_calls"):
                new_choice["finish_reason"] = "tool_calls"
            new_choices.append(new_choice)
    out["choices"] = new_choices
    if changed_any:
        names = []
        for choice in new_choices:
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
        if names:
            extra = f" (file_path→{current_path})" if current_path else ""
            sys.stderr.write(
                f"{LOG_PREFIX} promoted/rewrote tool_calls: {', '.join(names)}{extra}\n"
            )
    return out


def _upstream_target(path: str) -> tuple[str, int, str]:
    parsed = urlparse(UPSTREAM)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    base = parsed.path.rstrip("/")
    full_path = f"{base}{path}" if base else path
    return host, port, full_path


def _forward_upstream(
    method: str, path: str, body: bytes, headers: dict[str, str]
) -> tuple[int, list[tuple[str, str]], bytes]:
    host, port, upstream_path = _upstream_target(path)
    fwd = {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP
    }
    # Always target the upstream MAX listener, never the client-facing proxy port.
    fwd["Host"] = f"{host}:{port}" if port not in (80, 443) else host
    if method == "POST" and upstream_path.rstrip("/").endswith("/chat/completions"):
        fwd["Accept"] = "application/json"
        fwd["Expect"] = ""
    conn = HTTPConnection(host, port, timeout=600)
    try:
        conn.request(method, upstream_path, body=body or None, headers=fwd)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        resp_headers = list(resp.getheaders())
        return status, resp_headers, raw
    finally:
        conn.close()


def _maybe_promote_chat_completion(
    method: str, path: str, body: bytes, raw: bytes
) -> bytes:
    if (
        method != "POST"
        or not path.rstrip("/").endswith("/chat/completions")
        or not raw
    ):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    if not text.lstrip().startswith("{"):
        return raw
    try:
        payload = json.loads(text)
        req_payload = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    if req_payload.get("stream") or not isinstance(payload, dict):
        return raw
    return json.dumps(
        promote_chat_completion(payload, req_payload), ensure_ascii=False
    ).encode("utf-8")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{LOG_PREFIX} {self.address_string()} - {fmt % args}\n")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _client_headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items()}

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        path = urlparse(self.path).path or self.path
        if not path.startswith("/v1/"):
            self.send_error(404, f"unsupported path: {path}")
            return

        body = self._read_body() if method == "POST" else b""
        body = _maybe_rewrite_request_body(method, path, body)
        try:
            status, resp_headers, raw = _forward_upstream(
                method, path, body, self._client_headers()
            )
        except (HTTPException, OSError) as exc:
            sys.stderr.write(f"{LOG_PREFIX} upstream error: {exc}\n")
            self.send_error(502, f"upstream error: {exc}")
            return

        out = _maybe_promote_chat_completion(method, path, body, raw)

        self.send_response(status)
        for key, value in resp_headers:
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        path = urlparse(self.path).path or self.path
        if path == PROXY_HEALTH_PATH:
            self._send_json(200, {"status": "ok", "proxy": "qwen25-max-tool-call"})
            return
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    global UPSTREAM
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        default="http://127.0.0.1:8000",
        help="MAX serve base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    sys.stderr.write(
        f"{LOG_PREFIX} listening on http://{args.host}:{args.port} -> {UPSTREAM}\n"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
