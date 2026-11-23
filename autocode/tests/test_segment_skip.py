"""切题删行：`skip_lines` 只能整行删，且分块坐标必须跟着重映射。

守的是两件容易一起坏的事：
1. 删了行但排版块还按旧坐标走 → 正文挂到别的小节下面；
2. 删过头把样例/正文删掉 → 数据生成时才发现题面缺东西。
"""

from __future__ import annotations

import unittest

from autocode.slim2.segment import _remap_blocks, _slice_statement
from autocode.slim2.validators import Draft, check_segments

LINES = [
    "题目描述 题解 提交记录",          # 1 页签
    "3. 无重复字符的最长子串",          # 2 标题
    "已解答",                          # 3 状态
    "中等 相关标签 相关企业 提示",      # 4 状态
    "给定一个字符串 s，找出最长子串。",  # 5 正文
    "【样例输入】",                     # 6
    "abcabcbb",                        # 7
    "【样例输出】",                     # 8
    "3",                               # 9
    "提示：",                          # 10
    "0 <= s.length <= 10^5",           # 11
    "1 class Solution {",              # 12 代码区
]


def _item(**over):
    base = {"title": "无重复字符的最长子串", "start_line": 2, "end_line": 11,
            "judge_type": "token", "skip_lines": [3, 4]}
    base.update(over)
    return base


class TestSliceStatement(unittest.TestCase):
    def test_skip_lines_are_removed(self) -> None:
        body, mapping, removed = _slice_statement(_item(), LINES)
        text = "\n".join(body)
        self.assertNotIn("已解答", text)
        self.assertNotIn("相关标签", text)
        self.assertEqual(removed, 2)
        self.assertEqual(body[0], "3. 无重复字符的最长子串")

    def test_content_is_untouched(self) -> None:
        body, _mapping, _removed = _slice_statement(_item(), LINES)
        text = "\n".join(body)
        self.assertIn("给定一个字符串 s", text)
        self.assertIn("abcabcbb", text)
        self.assertIn("0 <= s.length", text)

    def test_code_area_outside_range_is_not_included(self) -> None:
        body, _mapping, _removed = _slice_statement(_item(), LINES)
        self.assertNotIn("class Solution", "\n".join(body))

    def test_page_furniture_still_removed(self) -> None:
        lines = list(LINES)
        lines[4] = "第 3 页 共 9 页"
        body, _mapping, removed = _slice_statement(_item(), lines)
        self.assertEqual(removed, 3)


class TestRemapBlocks(unittest.TestCase):
    def test_coordinates_follow_removed_lines(self) -> None:
        _body, mapping, _removed = _slice_statement(_item(), LINES)
        # 旧相对坐标：2=已解答（被删） 4=正文 5=【样例输入】
        blocks = [{"kind": "para", "from": 2, "to": 2},
                  {"kind": "para", "from": 4, "to": 4},
                  {"kind": "heading", "from": 5, "to": 5}]
        out = _remap_blocks(blocks, mapping)
        self.assertEqual([(b["from"], b["to"]) for b in out], [(2, 2), (3, 3)])

    def test_fully_removed_block_is_dropped(self) -> None:
        _body, mapping, _removed = _slice_statement(_item(), LINES)
        out = _remap_blocks([{"kind": "para", "from": 2, "to": 2}], mapping)
        self.assertIsNone(out)

    def test_spanning_block_keeps_surviving_part(self) -> None:
        _body, mapping, _removed = _slice_statement(_item(), LINES)
        # 旧相对 2~5 里 2、3 是被删的噪声，活下来的是 4、5 → 新坐标 2、3
        out = _remap_blocks([{"kind": "para", "from": 2, "to": 5}], mapping)
        self.assertEqual((out[0]["from"], out[0]["to"]), (2, 3))


class TestCleanSkips(unittest.TestCase):
    def _payload(self, skips):
        return {"problems": [_item(skip_lines=skips)]}

    def test_out_of_range_dropped(self) -> None:
        draft = check_segments(self._payload([1, 3, 12]), len(LINES), 20)
        self.assertEqual(draft.items[0]["skip_lines"], [3])

    def test_title_line_cannot_be_skipped(self) -> None:
        draft = check_segments(self._payload([2, 3]), len(LINES), 20)
        self.assertNotIn(2, draft.items[0]["skip_lines"])

    def test_garbage_ignored(self) -> None:
        draft = check_segments(self._payload(["3", None, True, 4.9, 4]), len(LINES), 20)
        self.assertEqual(draft.items[0]["skip_lines"], [3, 4])

    def test_too_many_skips_warns(self) -> None:
        draft = check_segments(self._payload(list(range(3, 9))), len(LINES), 20)
        self.assertTrue(draft.ok, draft.fatal)
        self.assertTrue(any("超过一半" in w for w in draft.warnings))


if __name__ == "__main__":
    unittest.main()
