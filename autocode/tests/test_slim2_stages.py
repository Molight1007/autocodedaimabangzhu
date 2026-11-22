"""阶段调用参数守卫：只有机械对账的复核段才关思考。

背景（2026-11-21 服务器实测）：D5 复核 4 条对抗数据时，模型把 16384 token
全花在 reasoning 上，`finish_reason=length`，重试仍截断，整次提交白跑 90 秒。
复核是逐条对账而非解题，关掉思考既省 token 也不会把预算烧空；生成段仍保留
默认思考，因为设计用例、算期望输出需要推理。
"""

import unittest

from autocode.slim2 import stages


class TestStageThinking(unittest.TestCase):
    def test_all_stages_declare_thinking(self) -> None:
        for name, spec in stages.STAGES.items():
            self.assertIn(
                spec.thinking, ("default", "enabled", "disabled"),
                f"{name} 的 thinking 取值非法：{spec.thinking}",
            )

    def test_adversarial_review_disables_thinking(self) -> None:
        self.assertEqual(stages.ADVERSARIAL_REVIEW.thinking, "disabled")

    def test_segment_disables_thinking(self) -> None:
        """切题只标行号和块类型，不产期望输出；开着思考会把输出预算烧空。

        2026-11-22 实测：v3 提示词（多了 blocks）配默认思考时 completion 正好撞满
        8192、正文为空；关掉后同一份输入 completion 1826，5/5 题分块通过校验。
        """
        self.assertEqual(stages.SEGMENT.thinking, "disabled")

    def test_generation_stages_keep_default_thinking(self) -> None:
        # SEGMENT 不在此列：它不生成数据、不算期望输出，不需要推理链
        for spec in (stages.DATASET, stages.ADVERSARIAL):
            self.assertEqual(
                spec.thinking, "default",
                f"{spec.code} 是生成段，关思考会降低算期望输出的准确率",
            )


if __name__ == "__main__":
    unittest.main()
