#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_PROXY_PATH = Path(__file__).with_name("qwen25-max-tool-call-proxy.py")
_SPEC = importlib.util.spec_from_file_location("qwen25_max_tool_call_proxy", _PROXY_PATH)
assert _SPEC and _SPEC.loader
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)


class ExtractToolCallsTest(unittest.TestCase):
    def test_nested_arguments_in_tool_call_tags(self) -> None:
        content = (
            '<tool_call>\n{"name": "file_read", "arguments": '
            '{"file_path": "scripts/wait-for-http.sh", "start_line": 1, "end_line": 24}}\n'
            "</tool_call>"
        )
        calls = proxy.extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "file_read")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["file_path"], "scripts/wait-for-http.sh")

    def test_bare_json_with_nested_arguments(self) -> None:
        content = (
            '{"name": "code_review", "arguments": {"path": "<current_file_path>", '
            '"diff": "<current_file_diff>", "reviewer_id": "user"}}'
        )
        calls = proxy.extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "code_review")

    def test_code_comment_nested_comments_array(self) -> None:
        content = json.dumps(
            {
                "name": "code_comment",
                "arguments": {
                    "comments": [
                        {
                            "content": "API key leaked to stdout",
                            "existing_code": 'echo "API_KEY: ${API_KEY}"',
                            "category": "security",
                            "severity": "high",
                        }
                    ]
                },
            }
        )
        calls = proxy.extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "code_comment")

    def test_task_done_defaults_state(self) -> None:
        calls = proxy.extract_tool_calls("task_done")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"state": "DONE"},
        )

    def test_promote_chat_completion_sets_finish_reason(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"name": "file_read", "arguments": {"file_path": "a.sh"}}',
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        out = proxy.promote_chat_completion(payload)
        choice = out["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(len(choice["message"]["tool_calls"]), 1)


if __name__ == "__main__":
    unittest.main()
