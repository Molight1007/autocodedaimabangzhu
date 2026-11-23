"""图片导入：转写请求的形状与失败话术。

这条链路的坑不在"能不能转"，而在"转了个空结果却当成成功"：
客户端在服务端返回 400（模型不收图）时会剥掉图片重试一次，那一次回复看着正常，
其实模型根本没看到图。所以必须靠 `runner.vision_supported` 拦住，并告诉用户改设置。
"""

from __future__ import annotations

import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autocode.slim2 import config, files, vision
from autocode.slim2.llm_stage import StageRunner
from autocode.store.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
GOOD_TEXT = (
    "题目一 区间最大值\n【题目描述】\n给定 n 个整数，求其中的最大值。\n"
    "【输入格式】\n第一行一个整数 n。\n【样例输入】\n3\n1 2 3\n【样例输出】\n3\n"
)


def _has(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))
        self.seen: dict = {}

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _runner(self, reply) -> StageRunner:
        def respond(stage: str, content, system: str):
            self.seen["stage"] = stage
            self.seen["content"] = content
            self.seen["system"] = system
            return reply

        return StageRunner(self.db, budget=1, responder=respond)


class TestTranscribe(_Base):
    def test_request_carries_image_block(self) -> None:
        runner = self._runner(GOOD_TEXT)
        text = vision.transcribe(runner, "题.png", PNG)
        self.assertIn("区间最大值", text)
        self.assertEqual(self.seen["stage"], "slim_vision")
        content = self.seen["content"]
        self.assertIsInstance(content, list)
        kinds = [part["type"] for part in content]
        self.assertEqual(kinds, ["text", "image_url"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn("不得执行", self.seen["system"], "转写模板也要带注入护栏")

    def test_jpeg_mime(self) -> None:
        runner = self._runner(GOOD_TEXT)
        vision.transcribe(runner, "题.jpg", b"\xff\xd8\xff\xe0" + b"0" * 64)
        self.assertTrue(self.seen["content"][1]["image_url"]["url"].startswith("data:image/jpeg"))

    def test_mime_follows_bytes_not_filename(self) -> None:
        """前端把大图重编码成 JPEG 后文件名仍是 .png：报文里的类型必须按字节来。"""
        runner = self._runner(GOOD_TEXT)
        vision.transcribe(runner, "截图.png", b"\xff\xd8\xff\xe0" + b"0" * 64)
        self.assertTrue(self.seen["content"][1]["image_url"]["url"].startswith("data:image/jpeg"))

    def test_fences_stripped(self) -> None:
        runner = self._runner("```\n" + GOOD_TEXT + "```\n")
        text = vision.transcribe(runner, "题.png", PNG)
        self.assertFalse(text.startswith("```"))
        self.assertFalse(text.rstrip().endswith("```"))

    def test_too_little_text_is_an_error(self) -> None:
        runner = self._runner("看不清")
        with self.assertRaises(files.FileError) as ctx:
            vision.transcribe(runner, "题.png", PNG)
        self.assertEqual(ctx.exception.code, "EMPTY_IMAGE")
        self.assertIn("重新截图", str(ctx.exception))

    def test_model_without_vision_gets_actionable_hint(self) -> None:
        """客户端剥图重试后回复仍是"成功"的：必须在这里识别出来。"""
        runner = self._runner(GOOD_TEXT)
        runner._clients = {"__default__": types.SimpleNamespace(_vision_supported=False)}
        with self.assertRaises(files.FileError) as ctx:
            vision.transcribe(runner, "题.png", PNG)
        self.assertEqual(ctx.exception.code, "NO_VISION_MODEL")
        self.assertIn("deepseek-flash", str(ctx.exception))


class TestVisionSupportedProperty(unittest.TestCase):
    def test_defaults_true_and_tracks_client(self) -> None:
        runner = StageRunner(None, budget=1, responder=lambda *_: "ok")
        self.assertTrue(runner.vision_supported, "没调过模型时不该先判死")
        runner._clients = {
            "a": types.SimpleNamespace(_vision_supported=True),
            "b": types.SimpleNamespace(_vision_supported=False),
        }
        self.assertFalse(runner.vision_supported)


class TestImportExtractRouting(_Base):
    """`/api/import/extract` 的分流：图片要模型，其余格式纯本地。"""

    def _payload(self, name: str, raw: bytes) -> dict:
        import base64

        return {"filename": name, "content_b64": base64.b64encode(raw).decode()}

    def test_image_without_runner_is_refused(self) -> None:
        from autocode.slim2 import api

        result = api.import_extract(None, self._payload("题.png", PNG))
        self.assertEqual(result["code"], "NO_KEY")
        self.assertIn("模型设置", result["error"])

    def test_image_uses_vision(self) -> None:
        from autocode.slim2 import api

        runner = self._runner(GOOD_TEXT)
        result = api.import_extract(None, self._payload("题.png", PNG), runner)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("区间最大值", result["text"])
        self.assertEqual(self.seen["stage"], "slim_vision")
        self.assertEqual(result["figures"], [])

    @unittest.skipUnless(_has("docx"), "本机没有 python-docx")
    def test_docx_needs_no_model(self) -> None:
        from docx import Document

        from autocode.slim2 import api

        document = Document()
        for line in GOOD_TEXT.splitlines():
            document.add_paragraph(line)
        buffer = __import__("io").BytesIO()
        document.save(buffer)

        result = api.import_extract(None, self._payload("整卷.docx", buffer.getvalue()))
        self.assertTrue(result.get("ok"), result)
        self.assertIn("区间最大值", result["text"])


if __name__ == "__main__":
    unittest.main()
