#!/usr/bin/env python3
"""Qwen 2.5 + MAX: promote Hermes-style tool text to OpenAI tool_calls.

When Qwen2.5 is served via local MAX, tool invocations often appear as plain
text, e.g.::

    <tool_call>{"name": "file_read", "arguments": {...}}</tool_call>

or a bare ``task_done`` line. OCR only acts on structured ``tool_calls`` in the
API response. This proxy forwards to MAX and rewrites non-streaming chat
completion responses when ``tool_calls`` is empty but the content looks like
a Qwen2.5 Hermes tool call.
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


def _normalize_tool_args(name: str, args: Any) -> Any:
    if name != "task_done":
        return args
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


def _make_tool_call(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("tool call missing name")
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

    stripped = _strip_markdown_fences(content)
    if not calls and stripped in {"task_done", "Task done", "TASK_DONE"}:
        _append_tool_call(calls, seen, {"name": "task_done", "arguments": {"state": "DONE"}})

    return calls


def promote_message(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("tool_calls"):
        return message
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return message
    tool_calls = extract_tool_calls(content)
    if not tool_calls:
        return message
    promoted = dict(message)
    promoted["tool_calls"] = tool_calls
    cleaned = TOOL_CALL_BLOCK_RE.sub("", content).strip()
    cleaned = _strip_markdown_fences(cleaned)
    if cleaned in {"", "task_done", "Task done", "TASK_DONE"}:
        promoted["content"] = None
    else:
        promoted["content"] = cleaned or None
    return promoted


def promote_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload
    out = dict(payload)
    new_choices = []
    promoted_any = False
    for choice in choices:
        if not isinstance(choice, dict):
            new_choices.append(choice)
            continue
        msg = choice.get("message")
        if not isinstance(msg, dict):
            new_choices.append(choice)
            continue
        promoted = promote_message(msg)
        if promoted is msg:
            new_choices.append(choice)
        else:
            promoted_any = True
            new_choice = dict(choice)
            new_choice["message"] = promoted
            if promoted.get("tool_calls"):
                new_choice["finish_reason"] = "tool_calls"
            new_choices.append(new_choice)
    out["choices"] = new_choices
    if promoted_any:
        names = []
        for choice in new_choices:
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
        if names:
            sys.stderr.write(
                f"{LOG_PREFIX} promoted tool_calls: {', '.join(names)}\n"
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
    return json.dumps(promote_chat_completion(payload), ensure_ascii=False).encode("utf-8")


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
