"""复杂度声明审计的边界：允许 log 因子带来的那一层嵌套。

背景（2026-11-19 实测 #11 移动 / #18 模板展开）：
`O(n log A)` 的标准写法就是两层循环（外层扫 n，内层二分 log A），
旧判据只看「声明的 n 次幂」，把它估成 1 阶，再用循环层数 2 判死 →
正经的对数解被当成换壳假货剔除 → 只剩 1 种解法 → 整题被产物门拦下回滚。
"""

from __future__ import annotations

import unittest

from autocode.app.prep_models import RefArtifact
from autocode.app.ref_audit import allowed_depth, audit_claims, claimed_degree, loop_depth


class NoteSink:
    """只收 note 的桩服务（audit_claims 只要 _note）。"""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def _note(self, text: str) -> None:
        self.notes.append(text)


ONE_LAYER = """import sys
n = int(sys.stdin.readline())
a = list(map(int, sys.stdin.readline().split()))
print(sum(a))
"""

TWO_LAYER = """import sys
n = int(sys.stdin.readline())
a = list(map(int, sys.stdin.readline().split()))
best = 0
for i in range(n):
    cur = 0
    for j in range(i, i + 2):
        cur += a[j % n]
    best = max(best, cur)
print(best)
"""

THREE_LAYER = """import sys
n = int(sys.stdin.readline())
a = list(map(int, sys.stdin.readline().split()))
best = 0
for i in range(n):
    for j in range(n):
        cur = 0
        for k in range(2):
            cur += a[(i + j + k) % n]
        best = max(best, cur)
print(best)
"""


class TestAllowedDepth(unittest.TestCase):
    def test_log_factor_gets_one_extra_level(self) -> None:
        self.assertEqual(allowed_depth("O(n log A)"), 2)
        self.assertEqual(allowed_depth("O(n log n)"), 2)
        self.assertEqual(allowed_depth("O(n^2 log n)"), 3)

    def test_plain_degree_has_no_extra_level(self) -> None:
        self.assertEqual(allowed_depth("O(n)"), 1)
        self.assertEqual(allowed_depth("O(n^2)"), 2)

    def test_unknown_is_none(self) -> None:
        self.assertIsNone(allowed_depth(""))
        self.assertIsNone(allowed_depth("O(1)"))
        self.assertIsNone(allowed_depth("未声明"))

    def test_degree_is_unchanged(self) -> None:
        self.assertEqual(claimed_degree("O(n log A)"), 1)
        self.assertEqual(loop_depth(TWO_LAYER), 2)


class TestAuditClaims(unittest.TestCase):
    def _refs(self, alt_code: str, alt_claim: str, base_code: str, base_claim: str):
        return {
            "python/reference": RefArtifact(
                lang="python", role="reference", code=base_code,
                complexity=base_claim, is_primary=1, approach="模拟",
            ),
            "python/alt1": RefArtifact(
                lang="python", role="alt1", code=alt_code,
                complexity=alt_claim, approach="数学",
            ),
        }

    def test_log_solution_with_two_loops_is_kept(self) -> None:
        """外层 n + 内层二分 = 2 层嵌套，但声明 O(n log A) 本来就允许 2 层。"""
        refs = self._refs(TWO_LAYER, "O(n log A)", TWO_LAYER, "O(n^2)")
        service = NoteSink()
        dropped = audit_claims(service, refs)
        self.assertEqual(dropped, [])
        self.assertIn("python/alt1", refs)
        self.assertTrue(any("保留" in n for n in service.notes))

    def test_three_loops_claiming_n_log_n_is_dropped(self) -> None:
        refs = self._refs(THREE_LAYER, "O(n log n)", TWO_LAYER, "O(n^2)")
        service = NoteSink()
        dropped = audit_claims(service, refs)
        self.assertEqual(dropped, ["python/alt1"])
        self.assertNotIn("python/alt1", refs)

    def test_two_loops_claiming_linear_is_dropped(self) -> None:
        refs = self._refs(TWO_LAYER, "O(n)", THREE_LAYER, "O(n^3)")
        service = NoteSink()
        dropped = audit_claims(service, refs)
        self.assertEqual(dropped, ["python/alt1"])

    def test_deeper_than_primary_is_dropped(self) -> None:
        """声称更低阶，嵌套却比主解还深 —— 明确不符。"""
        refs = self._refs(THREE_LAYER, "O(n log n)", TWO_LAYER, "O(n^2)")
        service = NoteSink()
        self.assertEqual(audit_claims(service, refs), ["python/alt1"])

    def test_not_claiming_better_is_untouched(self) -> None:
        refs = self._refs(THREE_LAYER, "O(n^3)", TWO_LAYER, "O(n^2)")
        self.assertEqual(audit_claims(NoteSink(), refs), [])


if __name__ == "__main__":
    unittest.main()
