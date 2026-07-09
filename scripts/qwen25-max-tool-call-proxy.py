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
JSON_OBJECT_RE = re.compile(
    r"\{[^{}]*\"name\"\s*:\s*\"[^\"]+\"[^{}]*\}", re.DOTALL
)

UPSTREAM = "http://127.0.0.1:8000"
LOG_PREFIX = "[qwen25-max-tool-call-proxy]"

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


def _make_tool_call(data: dict[str, Any]) -> dict[str, Any]:
    name = data.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("tool call missing name")
    args = data.get("arguments", data.get("parameters", {}))
    if isinstance(args, str):
        arg_str = args
    else:
        arg_str = json.dumps(args, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:16]}",
        "type": "function",
        "function": {"name": name, "arguments": arg_str},
    }


def extract_tool_calls(content: str) -> list[dict[str, Any]]:
    if not content:
        return []

    calls: list[dict[str, Any]] = []
    for match in TOOL_CALL_BLOCK_RE.finditer(content):
        block = match.group(1).strip()
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("name"):
            try:
                calls.append(_make_tool_call(data))
            except ValueError:
                continue

    if not calls:
        for match in JSON_OBJECT_RE.finditer(content):
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("name"):
                try:
                    calls.append(_make_tool_call(data))
                except ValueError:
                    continue

    stripped = content.strip()
    if not calls and stripped in {"task_done", "Task done", "TASK_DONE"}:
        calls.append(_make_tool_call({"name": "task_done", "arguments": {}}))

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
            new_choice = dict(choice)
            new_choice["message"] = promoted
            if promoted.get("tool_calls"):
                new_choice["finish_reason"] = "tool_calls"
            new_choices.append(new_choice)
    out["choices"] = new_choices
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


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{LOG_PREFIX} {self.address_string()} - {fmt % args}\n")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _client_headers(self) -> dict[str, str]:
        return {k: v for k, v in self.headers.items()}

    def _handle(self, method: str) -> None:
        body = self._read_body() if method == "POST" else b""
        try:
            status, resp_headers, raw = _forward_upstream(
                method, self.path, body, self._client_headers()
            )
        except (HTTPException, OSError) as exc:
            sys.stderr.write(f"{LOG_PREFIX} upstream error: {exc}\n")
            self.send_error(502, f"upstream error: {exc}")
            return

        out = raw
        ctype = ""
        for key, value in resp_headers:
            if key.lower() == "content-type":
                ctype = value.lower()
                break

        if (
            method == "POST"
            and self.path.rstrip("/").endswith("/chat/completions")
            and "application/json" in ctype
            and raw
        ):
            try:
                payload = json.loads(raw.decode("utf-8"))
                req_payload = json.loads(body.decode("utf-8")) if body else {}
                if not req_payload.get("stream") and isinstance(payload, dict):
                    out = json.dumps(
                        promote_chat_completion(payload), ensure_ascii=False
                    ).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                out = raw

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
