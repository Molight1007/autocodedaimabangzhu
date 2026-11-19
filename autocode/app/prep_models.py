"""预生成流水线的数据结构与常量（被 prep_service / prep_store / prep_rules 共用）。

拆出来的原因：`prep_service.py` 只放编排逻辑，文件行数受 R4 门禁约束（≤800 行），
把纯数据类与词表单独成文件，同时让测试可以只 import 数据结构而不触发编排依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

STAGE_SPEC = "spec"
STAGE_GENERATOR = "generator"
STAGE_REFERENCES = "references"
STAGE_CROSS_CHECK = "cross_check"
STAGE_DATASETS = "datasets"
STAGE_HINTS = "hints"

STAGE_ORDER: tuple[str, ...] = (
    STAGE_SPEC,
    STAGE_GENERATOR,
    STAGE_REFERENCES,
    STAGE_CROSS_CHECK,
    STAGE_DATASETS,
    STAGE_HINTS,
)

STATUS_DRAFT = "draft"
STATUS_PREPARING = "preparing"
STATUS_PREPARED = "prepared"
STATUS_FAILED = "failed"

#: 产物版本：产物结构或流程一变就 +1，与 `problems.artifact_version` 配对使用。
#:
#: 为什么要有它：`status='prepared'` 只说明"跑完过"，不说明"用哪版流程跑的"。
#: 旧版产物和新版产物在库里长得一模一样，而批量选题默认跳过 prepared 的题
#: ⇒ 改完流程后旧题永远不会被升级，用户看到的就是「代码改了、界面还是旧的」。
#: 把「新/旧」变成可判定的数据，才能让 `prepare --all` 自动增量升级旧版产物。
#: v4 = LLM 数据计划 + 2~4 种解法 + 四级提示 + 逐行对照代码解析。
ARTIFACT_VERSION = 4

OUTPUT_SINGLE = "single_value"
OUTPUT_LIST = "list"
OUTPUT_ANY = "any_solution"
OUTPUT_FLOAT = "float_eps"
ALLOWED_OUTPUT_TYPES: tuple[str, ...] = (OUTPUT_SINGLE, OUTPUT_LIST, OUTPUT_ANY, OUTPUT_FLOAT)

DEFAULT_MAX_CALLS = 40
#: 单题 token 上限（prompt+completion，含失败重试）。
#: 取值依据：5 道成功题的单轮实际用量 9.2k~16.5k，中位数约 15k；按用户要求留 10 倍余量 → 150k。
#: 它不是"防卡死"（那由 timeout 与子进程超时负责），而是**成本保险丝**：
#: 防止空返回/截断/重试循环把单题烧到几十万（曾出现单题累计 183k）。
DEFAULT_TOKEN_CAP = 150_000
# 造 oracle 时的宽限时限：Python 参考解在最大数据上常超题面时限，这里要的是"算得出来"
ORACLE_TIMEOUT_MS = 20_000
SAMPLE_TIMEOUT_MS = 10_000
GENERATOR_TIMEOUT_MS = 20_000  # 上限题（n=1e5）Python 生成器 5s 不够；10s 仍能挡住死循环

MAX_SPEC_ATTEMPTS = 3
MAX_GENERATOR_ATTEMPTS = 8
MAX_REF_ATTEMPTS = 8
MAX_BRUTE_ATTEMPTS = 2
MAX_HINTS_ATTEMPTS = 2


class BudgetExhausted(RuntimeError):
    """LLM 调用超预算（R8 熔断）。"""


class StageFailure(RuntimeError):
    """某阶段门未通过；携带阶段名与错误原文。"""

    def __init__(self, stage: str, message: str, *, detail: str = "") -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message
        self.detail = detail


@dataclass
class Budget:
    """调用预算：次数 + token 统计（熔断只看次数，token 只记录）。"""

    max_calls: int = DEFAULT_MAX_CALLS
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    def ensure(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"LLM 调用已达上限 {self.max_calls} 次（Budget 熔断）")

    def record(self, usage: Any = None, *, calls: int = 1) -> None:
        """记一次（或多次，重试算多次）调用；token 只按服务端返回的 usage 累加。"""
        self.calls += max(1, int(calls))
        if usage is not None:
            self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            self.reasoning_tokens += int(getattr(usage, "reasoning_tokens", 0) or 0)

    def to_dict(self) -> Dict[str, int]:
        return {
            "max_calls": self.max_calls,
            "calls": self.calls,
            "remaining": self.remaining,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


@dataclass
class StageRecord:
    stage: str
    ok: bool
    elapsed_ms: float
    detail: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class RefArtifact:
    """一份参考解/暴力解的落库状态。"""

    lang: str
    role: str
    code: str
    verified: int = 0
    verification_level: str = "reference_only"
    degrade_reason: str = ""
    samples_passed: int = 0
    samples_total: int = 0
    cross_ok: bool = False
    verify_record: Dict[str, Any] = field(default_factory=dict)
    approach: str = ""
    complexity: str = ""
    is_primary: int = 0
    line_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lang": self.lang,
            "role": self.role,
            "verified": self.verified,
            "approach": self.approach,
            "complexity": self.complexity,
            "is_primary": self.is_primary,
            "level": self.verification_level,
            "samples": f"{self.samples_passed}/{self.samples_total}",
            "cross_ok": self.cross_ok,
            "code_chars": len(self.code),
            "degrade_reason": self.degrade_reason,
        }


@dataclass
class PrepReport:
    problem_id: int
    ok: bool = False
    failed_stage: str = ""
    error: str = ""
    stages: List[StageRecord] = field(default_factory=list)
    spec: Dict[str, Any] = field(default_factory=dict)
    generator_ok: bool = False
    refs: List[RefArtifact] = field(default_factory=list)
    layers: Dict[str, int] = field(default_factory=dict)
    hints: List[Dict[str, Any]] = field(default_factory=list)
    budget: Dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    elapsed_ms: float = 0.0
    notes: List[str] = field(default_factory=list)
    injection_followed: bool = False
    injection_hits_in_code: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "ok": self.ok,
            "failed_stage": self.failed_stage,
            "error": self.error,
            "stages": [stage.to_dict() for stage in self.stages],
            "spec": self.spec,
            "generator_ok": self.generator_ok,
            "refs": [ref.to_dict() for ref in self.refs],
            "layers": self.layers,
            "hints": self.hints,
            "budget": self.budget,
            "llm_calls": self.llm_calls,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "notes": self.notes,
            "injection_followed": self.injection_followed,
            "injection_hits_in_code": self.injection_hits_in_code,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 分阶段思考策略：(thinking_mode, budget_tokens)
#: 结构化抽取/文本生成不需要长推理，关掉可省 60%+ 输出 token；
#: 代码生成保留推理但设上限（实测 v4-flash 默认 reasoning 可吃掉上万 token）。
THINKING_POLICY: Dict[str, Tuple[str, int]] = {
    "spec": ("disabled", 0),
    "hints": ("disabled", 0),
    "brute": ("disabled", 0),
    "generator": ("disabled", 0),
    "reference": ("disabled", 0),
    "parser": ("disabled", 0),
    "plan": ("disabled", 0),
    "data_plan": ("disabled", 0),
    "explanation": ("disabled", 0),
}


#: 分阶段温度策略（与 THINKING_POLICY 同风格）：显式传 temperature 时以调用点为准。
#: 口径：
#: - 结构化抽取 / 代码生成 / 判题相关 → 0.0（同输入要同输出，可复现、可审计）；
#: - 教学文本（hints/explanation）→ 0.4（要一点表达多样性，但不能跑题）；
#: - 解析类补充（method_analysis/code_notes）→ 0.2（贴着既有代码讲，少发挥）；
#: - plan（列解法清单）→ 0.3；diagnose（错因诊断）→ 0.2（要指向具体证据）。
TEMPERATURE_POLICY: Dict[str, float] = {
    "spec": 0.0,
    "generator": 0.0,
    "reference": 0.0,
    "brute": 0.0,
    "parser": 0.0,
    "hints": 0.4,
    "explanation": 0.4,
    "method_analysis": 0.2,
    "code_notes": 0.2,
    "plan": 0.3,
    "data_plan": 0.2,
    "diagnose": 0.2,
}

#: 未命中策略表时的兜底温度（历史默认值，保持向后兼容）。
DEFAULT_TEMPERATURE = 0.2

#: stage_key 的识别顺序：长前缀在前，避免 `plan` 抢走 `planner`、`reference` 抢走自身以外的东西。
#: `agent*` 是 ref_agent 的试跑模式（tag=`agent_python#1`），策略与 reference 相同；
#: `student_diagnose` 与 `diagnose` 归一到同一策略键。
_STAGE_PREFIXES: tuple[str, ...] = (
    "method_analysis",
    "code_notes",
    "student_diagnose",
    "data_plan",
    "diagnose",
    "explanation",
    "reference",
    "generator",
    "parser",
    "brute",
    "hints",
    "spec",
    "plan",
    "agent",
)


def stage_key(tag: str) -> str:
    """从 tag（如 `reference_python#2`）提取策略键（温度/思考策略共用）。"""
    head = str(tag or "").split("#")[0]
    if head.startswith("alt"):
        return "reference"
    for key in _STAGE_PREFIXES:
        if head.startswith(key):
            # 归一口径：student_diagnose → diagnose；agent（ref_agent 试跑）→ reference
            if key == "student_diagnose":
                return "diagnose"
            if key == "agent":
                return "reference"
            return key
    return ""
