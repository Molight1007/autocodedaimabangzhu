"""prepared 的前置门：参考解必须全部通过对拍认证。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from autocode.app.prep_rules import references_ready


@dataclass
class _Artifact:
    lang: str
    role: str
    verified: int


class TestReferencesReady(unittest.TestCase):
    def test_all_verified(self) -> None:
        refs = {
            "python/reference": _Artifact("python", "reference", 1),
            "cpp/reference": _Artifact("cpp", "reference", 1),
            "python/brute": _Artifact("python", "brute", 1),
        }
        self.assertEqual(references_ready(refs), (True, []))

    def test_one_conflict_blocks(self) -> None:
        refs = {
            "python/reference": _Artifact("python", "reference", 1),
            "cpp/reference": _Artifact("cpp", "reference", 0),
        }
        ok, missing = references_ready(refs)
        self.assertFalse(ok)
        self.assertEqual(missing, ["cpp/reference"])

    def test_brute_alone_is_not_enough(self) -> None:
        # 只有暴力解通过不能放行：参考解才是标准答案来源
        refs = {"python/brute": _Artifact("python", "brute", 1)}
        ok, missing = references_ready(refs)
        self.assertFalse(ok)
        self.assertEqual(missing, ["<无参考解>"])


if __name__ == "__main__":
    unittest.main()
