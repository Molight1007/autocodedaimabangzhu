"""起点吸附：模型给的 start_line 必须能被题干标题行纠正。

这是"图挂错题"的根因修复——行号偏几十行时，题面看着正常，图却按页码跑到了
隔壁那道题上。测试要盯住两件事：能吸附的必须吸附，不该吸附的绝不乱动。
"""

from __future__ import annotations

import unittest

from autocode.slim2.segment import SNAP_WINDOW, snap_to_headings

DOC = """封面信息
目录
仓库规划（plan）
第一题正文第一行
第一题正文第二行
因子化简（prime）
第二题正文第一行
第二题正文第二行
第32 次认证宝藏（treasure）
宝藏（treasure）
第三题正文第一行
第三题正文第二行
""".split("\n")


def _doc(extra: int = 0) -> list:
    """在第一个标题前插 extra 行占位，用来构造"模型把起点给偏了"的输入。"""
    return DOC[:2] + [f"填充{i}" for i in range(extra)] + DOC[2:]


class TestSnapToHeadings(unittest.TestCase):
    def test_snaps_backward_to_heading(self) -> None:
        lines = _doc()
        items = [
            {"title": "仓库规划（plan）", "start_line": 1, "end_line": 5},
            {"title": "因子化简（prime）", "start_line": 6, "end_line": 8},
            {"title": "宝藏（treasure）", "start_line": 9, "end_line": 12},
        ]
        snapped = snap_to_headings(items, lines)
        self.assertEqual(snapped, 2)  # 因子化简本来就在标题行上，不算"被吸附"
        self.assertEqual(items[0]["start_line"], 3)
        self.assertEqual(items[1]["start_line"], 6)
        self.assertEqual(items[2]["start_line"], 10)  # 页眉行不算锚点
        # 上一题的结束行跟着走，中间不留缝
        self.assertEqual(items[0]["end_line"], 5)
        self.assertEqual(items[1]["end_line"], 9)

    def test_unknown_slug_left_alone(self) -> None:
        items = [{"title": "不存在的题（ghost）", "start_line": 4, "end_line": 6}]
        self.assertEqual(snap_to_headings(items, DOC), 0)
        self.assertEqual(items[0]["start_line"], 4)

    def test_far_away_heading_ignored(self) -> None:
        items = [{"title": "仓库规划（plan）", "start_line": 3 + SNAP_WINDOW + 5, "end_line": 60}]
        self.assertEqual(snap_to_headings(items, DOC), 0)

    def test_out_of_order_snapping_abandoned(self) -> None:
        """两个锚点吸附后会倒序时，整批放弃，不制造重叠区间。"""
        lines = _doc()
        items = [
            {"title": "因子化简（prime）", "start_line": 3, "end_line": 5},
            {"title": "仓库规划（plan）", "start_line": 9, "end_line": 9},
        ]
        self.assertEqual(snap_to_headings(items, lines), 0)
        self.assertEqual([item["start_line"] for item in items], [3, 9])

    def test_heading_with_space_not_used_as_anchor(self) -> None:
        """页眉 `第32 次认证宝藏（treasure）` 不是锚点，真正的标题在下一行。"""
        lines = ["第32 次认证宝藏（treasure）", "宝藏（treasure）", "正文"]
        items = [{"title": "宝藏（treasure）", "start_line": 2, "end_line": 3}]
        snap_to_headings(items, lines)
        self.assertEqual(items[0]["start_line"], 2)


if __name__ == "__main__":
    unittest.main()
