"""LLM 客户端测试（不发网络请求）：凭据回退、JSON 双路径、推理模型空 content、重试退避。

用假传输替换 `_post`，因此不会真实计费；真实探针结果见交付报告。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.llm.client import (
    MIN_MAX_TOKENS,
    ChatUsage,
    LlmClient,
    LlmConfig,
    LlmConfigError,
    LlmError,
    extract_json_object,
    load_config,
    mask_key,
    read_key_file,
)


def _body(content: str, prompt_tokens: int = 10, completion_tokens: int = 5,
          reasoning: int = 0, finish: str = "stop") -> Dict[str, Any]:
    usage: Dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "model": "fake-model",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage,
    }


class FakeTransportClient(LlmClient):
    """按队列返回预置响应/异常，记录实际请求体与 sleep 调用。"""

    def __init__(self, responses: List[Any], *, sleep_calls: List[float] | None = None,
                 key_file: Path | None = None) -> None:
        super().__init__(
            LlmConfig(api_key="test-key", base_url="http://fake", model="fake-model", max_retries=3),
            sleep=(sleep_calls.append if sleep_calls is not None else (lambda _x: None)),
            key_file=key_file,
        )
        self.responses = list(responses)
        self.payloads: List[Dict[str, Any]] = []

    def _post(self, payload: Dict[str, Any], *, timeout_s: int) -> Dict[str, Any]:
        self.payloads.append(dict(payload))
        if not self.responses:
            raise AssertionError("没有更多预置响应")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestKeyHandling(unittest.TestCase):
    def test_mask_key(self) -> None:
        self.assertEqual(mask_key("sk-abcdefgh1234"), "****1234")
        self.assertEqual(mask_key(""), "<missing>")

    def test_env_key_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text("file-key-9999", encoding="utf-8")
            cfg = load_config({"DEEPSEEK_API_KEY": "env-key-1111"}, key_file=path)
            self.assertEqual(cfg.api_key, "env-key-1111")
            self.assertEqual(cfg.key_source, "env")

    def test_file_fallback_when_env_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text("  file-key-9999\n", encoding="utf-8")
            self.assertEqual(read_key_file(path), "file-key-9999")
            cfg = load_config({}, key_file=path)
            self.assertEqual(cfg.api_key, "file-key-9999")
            self.assertEqual(cfg.key_source, "file")

    def test_missing_both_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LlmConfigError):
                load_config({}, key_file=Path(tmp) / "nope.txt")

    def test_redacted_hides_key(self) -> None:
        cfg = LlmConfig(api_key="secret-value")
        dumped = json.dumps(cfg.redacted())
        self.assertNotIn("secret-value", dumped)
        self.assertIn("****", dumped)

    def test_401_switches_to_file_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text("file-key-9999", encoding="utf-8")
            client = FakeTransportClient(
                [LlmConfigError("HTTP 401（key=****1111）：invalid", status=401), _body('{"ok": true}')],
                key_file=path,
            )
            result = client.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(result.json_obj, {"ok": True})
            self.assertEqual(client.config.api_key, "file-key-9999")
            self.assertEqual(client.config.key_source, "file")


class TestExtractJson(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(extract_json_object('{"a": 1}'), {"a": 1})

    def test_fenced(self) -> None:
        self.assertEqual(extract_json_object('```json\n{"a": 2}\n```'), {"a": 2})

    def test_with_prose(self) -> None:
        self.assertEqual(extract_json_object('好的：\n{"a": 3}\n完成'), {"a": 3})

    def test_brace_balance_with_trailing_brace_in_prose(self) -> None:
        text = '说明 {"a": {"b": 1}} 后面还有 } 这种噪声'
        self.assertEqual(extract_json_object(text), {"a": {"b": 1}})

    def test_literal_newline_inside_string_is_tolerated(self) -> None:
        """提示词要求 L3 分步骤，模型常直接吐真实换行；严格模式会误判为“不是 JSON”。"""
        raw = '{"L3": "1. 读入\n2. 循环\n3. 输出"}'
        self.assertEqual(extract_json_object(raw), {"L3": "1. 读入\n2. 循环\n3. 输出"})

    def test_string_containing_braces(self) -> None:
        text = 'prefix {"a": "} not end", "b": 2} suffix'
        self.assertEqual(extract_json_object(text), {"a": "} not end", "b": 2})

    def test_invalid(self) -> None:
        self.assertIsNone(extract_json_object("not json"))


class TestChat(unittest.TestCase):
    def test_json_mode_sets_response_format(self) -> None:
        client = FakeTransportClient([_body('{"ok": true}')])
        result = client.chat([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertEqual(result.json_obj, {"ok": True})
        self.assertEqual(client.payloads[0]["response_format"], {"type": "json_object"})
        self.assertTrue(result.json_mode_used)

    def test_json_mode_400_falls_back_without_response_format(self) -> None:
        """服务端不支持 response_format 时，自动退回围栏提取，且后续请求不再带该字段。"""
        client = FakeTransportClient(
            [LlmConfigError("HTTP 400（key=****t-key）：response_format unsupported", status=400),
             _body('```json\n{"ok": true}\n```')]
        )
        result = client.chat([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertFalse(client._json_mode_supported)
        self.assertIn("response_format", client.payloads[0])
        self.assertNotIn("response_format", client.payloads[1])
        self.assertEqual(result.json_obj, {"ok": True})

    def test_max_tokens_floor(self) -> None:
        """推理模型：max_tokens 太小会被 reasoning 吃光，客户端必须抬到下限。"""
        client = FakeTransportClient([_body('{"ok": true}')])
        client.chat([{"role": "user", "content": "hi"}], max_tokens=10)
        self.assertEqual(client.payloads[0]["max_tokens"], MIN_MAX_TOKENS)

    def test_empty_content_retried_with_bigger_budget(self) -> None:
        delays: List[float] = []
        client = FakeTransportClient(
            [_body("", completion_tokens=10, reasoning=10, finish="length"), _body('{"ok": true}')],
            sleep_calls=delays,
        )
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.attempts, 2)
        # 被截断时先翻倍上限（至少 16384），到顶后再要求精简
        self.assertEqual(client.payloads[1]["max_tokens"], 16384)
        self.assertEqual(len(delays), 1)

    def test_empty_content_all_retries_raises(self) -> None:
        client = FakeTransportClient([_body("", reasoning=10)] * 4)
        with self.assertRaises(LlmError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.failures, 1)

    def test_retry_on_network_error_with_backoff(self) -> None:
        delays: List[float] = []
        client = FakeTransportClient(
            [LlmError("网络错误"), LlmError("网络错误"), _body('{"ok": true}')],
            sleep_calls=delays,
        )
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.attempts, 3)
        self.assertEqual(delays, [1.0, 2.0])

    def test_retry_on_invalid_json(self) -> None:
        delays: List[float] = []
        client = FakeTransportClient([_body("不是 JSON"), _body('{"ok": true}')], sleep_calls=delays)
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.json_obj, {"ok": True})
        self.assertEqual(len(delays), 1)

    def test_gives_up_after_retries(self) -> None:
        client = FakeTransportClient([LlmError("boom")] * 4)
        with self.assertRaises(LlmError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.failures, 1)

    def test_config_error_not_retried(self) -> None:
        client = FakeTransportClient([LlmConfigError("HTTP 403", status=403)])
        with self.assertRaises(LlmConfigError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(client.payloads), 1)

    def test_empty_messages_rejected(self) -> None:
        client = FakeTransportClient([])
        with self.assertRaises(LlmConfigError):
            client.chat([])


class TestUsage(unittest.TestCase):
    def test_reasoning_tokens_recorded(self) -> None:
        client = FakeTransportClient([_body('{"ok": true}', 100, 30, reasoning=22)])
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.usage.reasoning_tokens, 22)
        self.assertEqual(client.usage.to_dict()["reasoning_tokens"], 22)

    def test_usage_accumulated(self) -> None:
        client = FakeTransportClient([_body('{"a":1}', 100, 20), _body('{"a":2}', 50, 5)])
        client.chat([{"role": "user", "content": "1"}])
        client.chat([{"role": "user", "content": "2"}])
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.usage.prompt_tokens, 150)
        self.assertEqual(client.usage.completion_tokens, 25)
        self.assertEqual(client.usage.total_tokens, 175)

    def test_stats_masks_key(self) -> None:
        client = FakeTransportClient([_body('{"a":1}')])
        client.chat([{"role": "user", "content": "1"}])
        self.assertNotIn("test-key", json.dumps(client.stats()))

    def test_defaults(self) -> None:
        self.assertEqual(ChatUsage().to_dict()["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
