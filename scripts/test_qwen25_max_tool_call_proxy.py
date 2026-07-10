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
            '{"name": "code_search", "arguments": {"path": "<current_file_path>", '
            '"diff": "<current_file_diff>", "reviewer_id": "user"}}'
        )
        calls = proxy.extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "code_search")

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

    def test_rewrite_literal_current_file_path_placeholder(self) -> None:
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>\n"
                        "<current_file_diff>diff...</current_file_diff>"
                    ),
                }
            ]
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "file_read",
                                    "arguments": json.dumps(
                                        {
                                            "file_path": "<current_file_path>",
                                            "start_line": 5,
                                            "end_line": None,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        out = proxy.promote_chat_completion(payload, req)
        args = json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["file_path"], "scripts/wait-for-http.sh")
        self.assertNotIn("end_line", args)

    def test_extract_current_file_path(self) -> None:
        path = proxy.extract_current_file_path(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "<current_file_path>scripts/wait-for-http.sh</current_file_path>",
                    }
                ]
            }
        )
        self.assertEqual(path, "scripts/wait-for-http.sh")

    def test_normalize_leading_slash_path(self) -> None:
        self.assertEqual(
            proxy._normalize_repo_path(
                "/scripts/wait-for-http.sh", "scripts/wait-for-http.sh"
            ),
            "scripts/wait-for-http.sh",
        )
        self.assertEqual(
            proxy._normalize_repo_path(
                "./scripts/wait-for-http.sh", "scripts/wait-for-http.sh"
            ),
            "scripts/wait-for-http.sh",
        )
        # Without current_path, still strip the leading slash.
        self.assertEqual(
            proxy._normalize_repo_path("/scripts/wait-for-http.sh", None),
            "scripts/wait-for-http.sh",
        )

    def test_rewrite_leading_slash_file_path(self) -> None:
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>"
                    ),
                }
            ]
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "file_read",
                                    "arguments": json.dumps(
                                        {
                                            "file_path": "/scripts/wait-for-http.sh",
                                            "start_line": 1,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        out = proxy.promote_chat_completion(payload, req)
        args = json.loads(
            out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        self.assertEqual(args["file_path"], "scripts/wait-for-http.sh")

    def test_promote_review_prose_to_code_comment(self) -> None:
        essay = (
            "### Analysis of Changes in `scripts/wait-for-http.sh`\n\n"
            "#### Potential Issues and Suggestions:\n\n"
            "1. **Security Concerns**:\n"
            "    - The API key assignment directly in the script might expose the secret key.\n"
            "```bash\n"
            'API_KEY="sk-proj-9dF2aQx7bR4tYw8kZ1vN3mP6sL0hG5jU2cE"\n'
            "```\n"
            "2. **Suggested Fix**: use an environment variable instead.\n"
        )
        # Pad to clear the length threshold.
        essay = essay + ("More review detail. " * 20)
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>"
                    ),
                }
            ]
        }
        out = proxy.promote_chat_completion(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": essay},
                        "finish_reason": "stop",
                    }
                ]
            },
            req,
        )
        choice = out["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        tc = choice["message"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "code_comment")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["path"], "scripts/wait-for-http.sh")
        self.assertIn("API_KEY=", args["comments"][0].get("existing_code", ""))
        self.assertIsNone(choice["message"]["content"])

    def test_slash_task_done_promoted(self) -> None:
        for text in ("/task_done", "task_done", "Task Done", "\\task_done"):
            with self.subTest(text=text):
                calls = proxy.extract_tool_calls(text)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["function"]["name"], "task_done")
                self.assertEqual(
                    json.loads(calls[0]["function"]["arguments"]),
                    {"state": "DONE"},
                )

    def test_alias_code_review_current_file_to_file_read(self) -> None:
        req = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>"
                    ),
                }
            ]
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "code_review_current_file",
                                    "arguments": json.dumps(
                                        {
                                            "path": "scripts/wait-for-http.sh",
                                            "diff": "<current_file_diff>",
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        out = proxy.promote_chat_completion(payload, req)
        tc = out["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "file_read")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["file_path"], "scripts/wait-for-http.sh")
        self.assertNotIn("diff", args)
        self.assertNotIn("path", args)

    def test_promote_slash_task_done_content(self) -> None:
        out = proxy.promote_chat_completion(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "/task_done"},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        choice = out["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(
            choice["message"]["tool_calls"][0]["function"]["name"], "task_done"
        )
        self.assertIsNone(choice["message"]["content"])

    def test_clears_leftover_tool_json_and_dangling_tag(self) -> None:
        content = (
            '{"name": "file_read", "arguments": {"file_path": "scripts/wait-for-http.sh", '
            '"start_line": 1, "end_line": 5}}\n<tool_call>'
        )
        out = proxy.promote_chat_completion(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        msg = out["choices"][0]["message"]
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertIsNone(msg["content"])

    def test_rewrite_chat_request_injects_policy_once(self) -> None:
        req = {
            "messages": [
                {"role": "system", "content": "You are a reviewer."},
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>\n"
                        "review the diff"
                    ),
                },
            ]
        }
        out, changed = proxy.rewrite_chat_request(req)
        self.assertTrue(changed)
        system = out["messages"][0]["content"]
        self.assertIn(proxy.TOOL_POLICY_MARKER, system)
        self.assertIn("scripts/wait-for-http.sh", system)
        self.assertIn("<tool_call>", system)
        out2, changed2 = proxy.rewrite_chat_request(out)
        self.assertFalse(changed2)

    def test_rewrite_empty_tool_nudge_after_prose(self) -> None:
        essay = (
            "### Analysis\n\n#### Potential Issues and Suggestions:\n\n"
            "1. **Security Concerns**: hardcoded API_KEY.\n"
        ) + ("detail " * 80)
        req = {
            "messages": [
                {"role": "system", "content": "base"},
                {
                    "role": "user",
                    "content": (
                        "<current_file_path>scripts/wait-for-http.sh</current_file_path>"
                    ),
                },
                {"role": "assistant", "content": essay},
                {"role": "user", "content": proxy.OCR_EMPTY_TOOL_NUDGE},
            ]
        }
        out, changed = proxy.rewrite_chat_request(req)
        self.assertTrue(changed)
        nudge = out["messages"][-1]["content"]
        self.assertIn(proxy.STRONG_EMPTY_TOOL_NUDGE, nudge)
        self.assertNotIn(proxy.OCR_EMPTY_TOOL_NUDGE, nudge)
        self.assertIn("prose review", nudge)


if __name__ == "__main__":
    unittest.main()
