"""
集成测试：全量用例对拍验证核心逻辑。

不依赖 GUI，直接 import 模块。
模拟 3 个测试用例（第 3 个故意不一致），验证：
  1. _collect_test_cases() 返回全部 3 个用例
  2. _compare_codes_on_cases() 在第一个不匹配处停止
  3. run_cross_validation() 触发交叉反馈重试，重试后重新全量验证
"""

import os
import sys
import unittest
from unittest.mock import patch

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 两套 solve 代码：code_a 始终正确，code_b 在 a > 3 时故意乘而非加
# ---------------------------------------------------------------------------

CODE_A = r"""
def solve(problem, data):
    stdin = str(data.get('stdin', ''))
    parts = stdin.strip().split()
    a, b = int(parts[0]), int(parts[1])
    return {'stdout': f'{a + b}\n'}
"""

CODE_B_BUGGY = r"""
def solve(problem, data):
    stdin = str(data.get('stdin', ''))
    parts = stdin.strip().split()
    a, b = int(parts[0]), int(parts[1])
    if a <= 3:
        return {'stdout': f'{a + b}\n'}
    else:
        return {'stdout': f'{a * b}\n'}
"""

CODE_B_FIXED = r"""
def solve(problem, data):
    stdin = str(data.get('stdin', ''))
    parts = stdin.strip().split()
    a, b = int(parts[0]), int(parts[1])
    return {'stdout': f'{a + b}\n'}
"""

# 3 个测试用例：
#   case 0: a=1,b=2 → 两套代码都输出 "3\n"   ✓ 匹配
#   case 1: a=3,b=4 → 两套代码都输出 "7\n"   ✓ 匹配（a<=3 走同一分支）
#   case 2: a=5,b=6 → A="11\n", B="30\n"     ✗ 不匹配（首个不一致点）
# 格式：每行两个整数 a b（空格分隔），solve 读取 parts[0]=a, parts[1]=b
TEST_CASES = [
    {"stdin": "1 2\n", "stdout": "3\n"},
    {"stdin": "3 4\n", "stdout": "7\n"},
    {"stdin": "5 6\n", "stdout": "11\n"},
]

EXPECTED_COMPARED_BEFORE_STOP = 3   # 0,1 匹配，2 不匹配 → 共比较 3 个
EXPECTED_MATCHED = 2                # case 0,1 匹配


# ===================================================================
# 测试 1: _collect_test_cases 返回全量用例
# ===================================================================
class TestCollectTestCases(unittest.TestCase):
    def test_returns_all_cases_when_provided(self):
        """all_test_cases 传入 3 个用例时，_collect_test_cases 返回 3 个。"""
        from services.ai.autonomous_fix_worker import AutonomousFixWorker

        worker = AutonomousFixWorker(
            problem={},
            candidate_code="",
            llm_caller=lambda m: "",
            all_test_cases=TEST_CASES,
        )
        result = worker._collect_test_cases()
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["stdin"], "1 2\n")
        self.assertEqual(result[2]["stdin"], "5 6\n")

    def test_fallback_when_no_all_test_cases(self):
        """未传 all_test_cases 时回退到 runtime_context['data_samples']。"""
        from services.ai.autonomous_fix_worker import AutonomousFixWorker

        worker = AutonomousFixWorker(
            problem={},
            candidate_code="",
            llm_caller=lambda m: "",
            runtime_context={"data_samples": [{"stdin": "x"}]},
            # all_test_cases 不传
        )
        result = worker._collect_test_cases()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["stdin"], "x")

    def test_fallback_empty_when_nothing(self):
        """无任何数据源时返回空列表。"""
        from services.ai.autonomous_fix_worker import AutonomousFixWorker

        worker = AutonomousFixWorker(
            problem={},
            candidate_code="",
            llm_caller=lambda m: "",
        )
        result = worker._collect_test_cases()
        self.assertEqual(result, [])


# ===================================================================
# 测试 2: _compare_codes_on_cases 遇错即停
# ===================================================================
class TestCompareEarlyStop(unittest.TestCase):
    def test_stops_on_first_mismatch(self):
        """3 个用例，第 3 个不匹配 → 应在比较 3 个后停止，不会比较第 4 个。"""
        from services.isolated_agent.verifier.cross_runner import _compare_codes_on_cases

        # 多加 2 个用例——如果没停，compared_cases 会超过 3
        extra_cases = TEST_CASES + [
            {"stdin": "7 8\n", "stdout": "15\n"},
            {"stdin": "9 10\n", "stdout": "19\n"},
        ]

        result = _compare_codes_on_cases(
            code_a=CODE_A,
            code_b=CODE_B_BUGGY,
            test_cases=[{"stdin": c["stdin"]} for c in extra_cases],
            can_brute_force=False,
        )

        self.assertFalse(result["all_match"])
        self.assertEqual(result["total_cases"], 5)             # 全量共 5 个
        self.assertEqual(result["compared_cases"], 3)          # 只比较了前 3 个
        self.assertEqual(result["matched_cases"], 2)           # 前 2 个匹配
        self.assertEqual(len(result["mismatch_cases"]), 1)     # 只有 1 条不匹配记录
        self.assertNotEqual(result["first_mismatch"], {})
        first = result["first_mismatch"]
        self.assertIn("11", first["out_a"])                     # code_a 正确输出
        self.assertIn("30", first["out_b"])                     # code_b 错误输出

    def test_all_match_no_early_stop(self):
        """全部匹配时不触发遇错即停，compared_cases 等于 total_cases。"""
        from services.isolated_agent.verifier.cross_runner import _compare_codes_on_cases

        result = _compare_codes_on_cases(
            code_a=CODE_A,
            code_b=CODE_A,  # 两套相同
            test_cases=[{"stdin": c["stdin"]} for c in TEST_CASES],
            can_brute_force=False,
        )

        self.assertTrue(result["all_match"])
        self.assertEqual(result["compared_cases"], 3)
        self.assertEqual(result["matched_cases"], 3)
        self.assertEqual(len(result["mismatch_cases"]), 0)


# ===================================================================
# 测试 3: run_cross_validation 全量对拍 + 重试 + 重新全量验证
# ===================================================================
class TestRunCrossValidationFullFlow(unittest.TestCase):
    def test_full_flow_mismatch_retry_requeries_all(self):
        """
        完整流程：
        1. 首次对拍：case 0,1 匹配，case 2 不匹配 → 遇错即停
        2. 触发交叉反馈重试
        3. 重试后第二次对拍：全部 3 个都匹配 → 通过
        """
        from services.isolated_agent.verifier.cross_runner import run_cross_validation

        analysis = {
            "can_brute_force": False,
            "confidence": "high",
            "output_type": "single_value",
            "algorithms": [
                {"name": "two_pointer", "description": "双指针求和"},
                {"name": "hash_map", "description": "哈希表求和"},
            ],
        }

        # ---- mock generate_dual_codes：返回 buggy 的 code_b ----
        def mock_generate_dual_codes(**kwargs):
            return {
                "code_a": CODE_A,
                "code_b": CODE_B_BUGGY,
                "a_success": True,
                "b_success": True,
                "a_rounds": 5,
                "b_rounds": 5,
                "a_error": "",
                "b_error": "",
            }

        # ---- mock retry：模拟修复后 code_b 变正确 ----
        retry_call_count = [0]

        def mock_retry(slot, algorithm, problem, llm_caller_factory,
                       current_code, used_rounds, max_rounds, feedback):
            retry_call_count[0] += 1
            # 验证 feedback（不匹配信息）已正确传入
            self.assertIn("stdin", feedback)
            self.assertIn("out_a", feedback)
            self.assertIn("out_b", feedback)
            # 返回修复后的正确代码
            return {
                "success": True,
                "rounds": used_rounds + 2,
                "code": CODE_B_FIXED if slot == "b" else CODE_A,
                "error": "",
            }

        with patch(
            "services.isolated_agent.verifier.cross_runner.generate_dual_codes",
            side_effect=mock_generate_dual_codes,
        ), patch(
            "services.isolated_agent.verifier.cross_runner._retry_single_agent_after_mismatch",
            side_effect=mock_retry,
        ):
            result = run_cross_validation(
                problem={"title": "两数之和", "constraints": "1<=n<=1000"},
                analysis_result=analysis,
                test_cases=[{"stdin": c["stdin"]} for c in TEST_CASES],
                llm_caller_factory=lambda tag: lambda m: "{}",
                max_rounds=30,
            )

        # ---- 断言 ----
        # 重试被调用 2 次（slot a 和 slot b 各一次）
        self.assertEqual(retry_call_count[0], 2)

        # 验证通过
        self.assertTrue(result["verification_passed"])
        self.assertEqual(result["total_cases"], 3)
        self.assertEqual(result["matched_cases"], 3)
        self.assertEqual(result["reason"], "outputs_match")
        self.assertEqual(len(result["mismatch_cases"]), 0)
        self.assertEqual(result["strategy"], "cross_algo")

    def test_full_flow_mismatch_after_retry_still_fails(self):
        """重试后仍不匹配 → 降级返回。"""
        from services.isolated_agent.verifier.cross_runner import run_cross_validation

        analysis = {
            "can_brute_force": False,
            "confidence": "high",
            "output_type": "single_value",
            "algorithms": [
                {"name": "algo_a", "description": "算法A"},
                {"name": "algo_b", "description": "算法B"},
            ],
        }

        def mock_generate_dual_codes(**kwargs):
            return {
                "code_a": CODE_A,
                "code_b": CODE_B_BUGGY,
                "a_success": True,
                "b_success": True,
                "a_rounds": 3,
                "b_rounds": 3,
                "a_error": "",
                "b_error": "",
            }

        def mock_retry(**kwargs):
            # 重试后仍然返回 buggy 代码（模拟修复失败）
            return {
                "success": False,
                "rounds": kwargs.get("used_rounds", 0) + 2,
                "code": kwargs.get("current_code", ""),
                "error": "agent_generation_failed",
            }

        with patch(
            "services.isolated_agent.verifier.cross_runner.generate_dual_codes",
            side_effect=mock_generate_dual_codes,
        ), patch(
            "services.isolated_agent.verifier.cross_runner._retry_single_agent_after_mismatch",
            side_effect=mock_retry,
        ):
            result = run_cross_validation(
                problem={},
                analysis_result=analysis,
                test_cases=[{"stdin": c["stdin"]} for c in TEST_CASES],
                llm_caller_factory=lambda tag: lambda m: "{}",
                max_rounds=30,
            )

        self.assertFalse(result["verification_passed"])
        self.assertEqual(result["reason"], "mismatch_after_retry")
        self.assertGreater(len(result["mismatch_cases"]), 0)


# ===================================================================
# 测试 4: 数据传递链完整性（Worker → facade）
# ===================================================================
class TestDataPassingChain(unittest.TestCase):
    def test_all_test_cases_flow_to_collect(self):
        """验证 AutonomousFixWorker 构造函数 -> _collect_test_cases 链路。"""
        from services.ai.autonomous_fix_worker import AutonomousFixWorker

        worker = AutonomousFixWorker(
            problem={"title": "test"},
            candidate_code=CODE_A,
            llm_caller=lambda m: "{}",
            llm_caller_factory=lambda tag: lambda m: "{}",
            enable_cross_validation=True,
            all_test_cases=TEST_CASES,
        )
        # _collect_test_cases 应由 self._all_test_cases 驱动
        cases = worker._collect_test_cases()
        self.assertEqual(len(cases), 3,
                         "应返回全部 3 个用例，而非 data_samples 中的单个样例")


# ===================================================================
# 测试 5: data_pool 过滤层
# ===================================================================
class TestDataPool(unittest.TestCase):
    def test_all_cases_within_limits_passthrough(self):
        """全部用例在限制范围内 → 全部保留。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        cases = [{"stdin": "1 2\n"}, {"stdin": "3 4\n"}, {"stdin": "5 6\n"}]
        pool = prepare_cross_validation_pool(cases, max_stdin_chars=3000, max_pool_size=50)

        self.assertFalse(pool.is_empty)
        self.assertEqual(pool.total_original, 3)
        self.assertEqual(pool.total_retained, 3)
        self.assertEqual(pool.total_filtered_by_size, 0)
        self.assertEqual(pool.total_dropped_by_cap, 0)

    def test_filters_large_stdin(self):
        """stdin 超过 max_stdin_chars 的用例被过滤。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        small = {"stdin": "ab"}
        large = {"stdin": "x" * 5000}
        cases = [small, large, small.copy()]

        pool = prepare_cross_validation_pool(cases, max_stdin_chars=100, max_pool_size=50)

        self.assertFalse(pool.is_empty)
        self.assertEqual(pool.total_original, 3)
        self.assertEqual(pool.total_filtered_by_size, 1)
        self.assertEqual(pool.total_retained, 2)
        for c in pool.cases:
            self.assertLessEqual(len(c["stdin"]), 100)

    def test_all_filtered_out_empty_pool(self):
        """所有用例 stdin 都超长 → 池子为空。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        cases = [{"stdin": "x" * 200}, {"stdin": "y" * 300}]
        pool = prepare_cross_validation_pool(cases, max_stdin_chars=50, max_pool_size=50)

        self.assertTrue(pool.is_empty)
        self.assertEqual(pool.total_original, 2)
        self.assertEqual(pool.total_filtered_by_size, 2)
        self.assertEqual(pool.total_retained, 0)

    def test_caps_pool_size_random_sample(self):
        """超过 max_pool_size 时随机采样，保留数量不超过上限。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        cases = [{"stdin": f"case_{i}"} for i in range(100)]
        pool = prepare_cross_validation_pool(cases, max_stdin_chars=3000, max_pool_size=30, seed=42)

        self.assertFalse(pool.is_empty)
        self.assertEqual(pool.total_original, 100)
        self.assertEqual(pool.total_filtered_by_size, 0)
        self.assertEqual(pool.total_dropped_by_cap, 70)
        self.assertEqual(pool.total_retained, 30)

    def test_deterministic_with_seed(self):
        """相同 seed 产生相同的采样结果。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        cases = [{"stdin": f"case_{i}"} for i in range(50)]
        pool1 = prepare_cross_validation_pool(cases, max_stdin_chars=3000, max_pool_size=10, seed=123)
        pool2 = prepare_cross_validation_pool(cases, max_stdin_chars=3000, max_pool_size=10, seed=123)

        self.assertEqual([c["stdin"] for c in pool1.cases],
                         [c["stdin"] for c in pool2.cases])

    def test_empty_input(self):
        """空输入 → 池子为空。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        pool = prepare_cross_validation_pool([])
        self.assertTrue(pool.is_empty)
        self.assertEqual(pool.total_original, 0)

    def test_string_items_normalized(self):
        """str 类型的用例被规范化为 dict。"""
        from services.isolated_agent.verifier.data_pool import prepare_cross_validation_pool

        pool = prepare_cross_validation_pool(["1 2\n", "3 4\n"], max_stdin_chars=3000, max_pool_size=50)
        self.assertEqual(pool.total_retained, 2)
        self.assertIsInstance(pool.cases[0], dict)
        self.assertIn("stdin", pool.cases[0])

    def test_default_parameters(self):
        """不传参数时使用模块默认值。"""
        from services.isolated_agent.verifier.data_pool import (
            prepare_cross_validation_pool,
            DEFAULT_MAX_STDIN_CHARS,
            DEFAULT_MAX_POOL_SIZE,
        )
        # 构造刚好在默认限制内的用例
        cases = [{"stdin": "x" * (DEFAULT_MAX_STDIN_CHARS - 100)}]
        pool = prepare_cross_validation_pool(cases)
        self.assertEqual(pool.total_retained, 1)

        # 构造超过默认限制的用例
        pool2 = prepare_cross_validation_pool(
            [{"stdin": "x" * (DEFAULT_MAX_STDIN_CHARS + 1)}]
        )
        self.assertEqual(pool2.total_filtered_by_size, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
