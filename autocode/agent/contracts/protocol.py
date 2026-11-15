"""autoCode Agent 协议常量与类型约束。

核心职责：
- 定义阶段 1 冻结后的动作集合与运行结果类型，作为跨模块统一词表。
- 提供轻量常量，避免 controller/session 对字符串字面量产生分叉。

不负责：
- 不做请求校验（由 `contracts.validator` 负责）。
- 不管理会话状态（由 `agent.session` 负责）。
- 不做动作分发（由 `agent.controller.dispatcher` 负责）。

数据流：
- 外部入口请求 -> validator 读取本模块常量 -> dispatcher 按动作路由。
- run 执行结果 -> dispatcher 使用本模块结果类型生成统一回包。

约束：
- 本模块不引入业务逻辑，仅维护协议词表，避免协议漂移。
- 设计 §4 要求新增 run_case/compile/gen_data/cross_check/search/fetch 六个动作：
  届时 `ALLOWED_ACTIONS`、`ActionName`（闭合 Literal）、`RUN_RESULT_TYPES` 与
  `contracts.validator._VALIDATORS` 必须四处同步新增，否则单测与门禁即红。
  这是"只加不改"无法覆盖的破坏性变更，P0 暂不扩词表。
"""

from __future__ import annotations

from typing import Final, Literal

# 验证层词表的定义已下沉到零依赖叶子 `engine/verify/verdict.py`，本模块只做 re-export。
# 原因：`llm/analyzer.py` 需要这些词表，而 llm 层禁止反向 import agent（门禁 layer-llm-import）。
from ...engine.verify import verdict as _verify_verdict

ACTION_READ: Final[str] = "read"
ACTION_RUN: Final[str] = "run"
ACTION_MODIFY: Final[str] = "modify"
ACTION_FINALIZE: Final[str] = "finalize"
ACTION_SELF_TEST: Final[str] = "self_test"

ALLOWED_ACTIONS: Final[tuple[str, ...]] = (
    ACTION_READ,
    ACTION_RUN,
    ACTION_MODIFY,
    ACTION_FINALIZE,
    ACTION_SELF_TEST,
)

RESULT_RIGHT: Final[str] = "right"
RESULT_WRONG: Final[str] = "wrong"
RESULT_RUNTIME_ERROR: Final[str] = "runtime_error"
RESULT_GRAMMATICAL_ERROR: Final[str] = "grammatical_error"
RESULT_SYSTEM_ERROR: Final[str] = "system_error"
RESULT_READ_OK: Final[str] = "read_ok"
RESULT_MODIFY_OK: Final[str] = "modify_ok"
RESULT_FINALIZE_OK: Final[str] = "finalize_ok"
RESULT_SELF_TEST_OK: Final[str] = "self_test_ok"
RESULT_PROTOCOL_ERROR: Final[str] = "protocol_error"

RUN_RESULT_TYPES: Final[tuple[str, ...]] = (
    RESULT_RIGHT,
    RESULT_WRONG,
    RESULT_RUNTIME_ERROR,
    RESULT_GRAMMATICAL_ERROR,
    RESULT_SYSTEM_ERROR,
)

DEFAULT_MAX_AUTONOMOUS_ATTEMPTS: Final[int] = 30
DEFAULT_CONTEXT_ROUNDS: Final[int] = 15
MAX_CONTEXT_ROUNDS: Final[int] = 30
MAX_SAME_ACTION_ROUNDS: Final[int] = 5
PROBLEM_CONTEXT_MAX_CHARS: Final[int] = 10_000
CODE_CONTEXT_MAX_CHARS: Final[int] = 10_000
RUN_MODE_CURRENT: Final[str] = "current"
RUN_MODE_INLINE: Final[str] = "inline"

READ_TARGET_CODE: Final[str] = "code"
READ_TARGET_PROBLEM: Final[str] = "problem"
READ_TARGET_ALL: Final[str] = "all"
READ_TARGET_DATA_SAMPLE: Final[str] = "data_sample"
READ_TARGET_VERSION_PREFIX: Final[str] = "v"
MAX_VERSION_SNAPSHOTS: Final[int] = 5
ALLOWED_READ_TARGETS: Final[tuple[str, ...]] = (
    READ_TARGET_CODE,
    READ_TARGET_PROBLEM,
    READ_TARGET_ALL,
    READ_TARGET_DATA_SAMPLE,
)
DATA_SAMPLE_MAX_CHARS: Final[int] = 5000
SELF_TEST_MIN_CASES: Final[int] = 1
SELF_TEST_MAX_CASES: Final[int] = 5
SELF_TEST_TIMEOUT_MIN_SEC: Final[float] = 0.1
SELF_TEST_TIMEOUT_MAX_SEC: Final[float] = 10.0
SELF_TEST_DEFAULT_TIMEOUT_SEC: Final[float] = 2.0
SELF_TEST_STATUS_OK: Final[str] = "ok"
SELF_TEST_STATUS_RUNTIME_ERROR: Final[str] = "runtime_error"
SELF_TEST_STATUS_TIMEOUT: Final[str] = "timeout"
ALLOWED_SELF_TEST_STATUSES: Final[tuple[str, ...]] = (
    SELF_TEST_STATUS_OK,
    SELF_TEST_STATUS_RUNTIME_ERROR,
    SELF_TEST_STATUS_TIMEOUT,
)

ActionName = Literal["read", "run", "modify", "finalize", "self_test"]
RunResultType = Literal["right", "wrong", "runtime_error", "grammatical_error", "system_error"]

# ── 验证层常量（re-export，定义在 engine/verify/verdict.py）──────────────────
# 路由策略
VERIFY_STRATEGY_BRUTE: Final[str] = _verify_verdict.VERIFY_STRATEGY_BRUTE
VERIFY_STRATEGY_CROSS: Final[str] = _verify_verdict.VERIFY_STRATEGY_CROSS
VERIFY_STRATEGY_SKIP: Final[str] = _verify_verdict.VERIFY_STRATEGY_SKIP

# 算法分析置信度
VERIFY_CONFIDENCE_HIGH: Final[str] = _verify_verdict.VERIFY_CONFIDENCE_HIGH
VERIFY_CONFIDENCE_MEDIUM: Final[str] = _verify_verdict.VERIFY_CONFIDENCE_MEDIUM
VERIFY_CONFIDENCE_LOW: Final[str] = _verify_verdict.VERIFY_CONFIDENCE_LOW
ALLOWED_VERIFY_CONFIDENCES: Final[tuple[str, ...]] = _verify_verdict.ALLOWED_VERIFY_CONFIDENCES

# 输出类型
VERIFY_OUTPUT_SINGLE: Final[str] = _verify_verdict.VERIFY_OUTPUT_SINGLE
VERIFY_OUTPUT_LIST: Final[str] = _verify_verdict.VERIFY_OUTPUT_LIST
VERIFY_OUTPUT_ANY: Final[str] = _verify_verdict.VERIFY_OUTPUT_ANY
ALLOWED_VERIFY_OUTPUT_TYPES: Final[tuple[str, ...]] = _verify_verdict.ALLOWED_VERIFY_OUTPUT_TYPES
