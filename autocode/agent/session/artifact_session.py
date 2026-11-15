"""autoCode 产物会话（设计 §4 决策 8：一产物一会话）。

为什么不用 `AgentSessionState`（设计 §2 明确判定不复用）：
- 旧模型只有单 `current_code` + "run 过 / self_test 稳 / hash 对" 三闸门，
  接不住「每题 5 个产物」（generator + C/C++/Python 参考解 + 暴力解）：
  暴力解跑通会污染权威代码，参考解之间也无法各自持有证据。
- 因此每题开 5 个 `ArtifactSession`，各自持有 code / 版本 / 尝试记录 / 证据。

硬约束（本题核心不变式）：
- **一产物一会话，互不共享可变状态**。本模块只在实例自身状态上读写，
  不提供任何"按 problem_id 查找其它会话"、"批量写入"、"跨会话同步"的入口，
  从 API 形状上排除"跑通暴力解污染参考解"这类事故。
- 每个会话只在**当前代码版本**上积累证据；`set_code` 换码即清空证据（旧证据不再可信）。
- `passed` 只接受真正的 `bool`：它是唯一决定放行的字段，绝不做 `bool("false")` 这类
  真值转换（"false"/"0"/非空字符串/non-zero int 都会被错误地当成通过）。

产物级放行规则（`can_finalize`）：
- `generator`：已跑通（子进程执行成功）且 grammar 校验通过。
- `ref_c` / `ref_cpp` / `ref_py`（参考解）：**样例跑满应跑条数且全部通过** + 存在通过的对拍记录。
- `brute`（暴力解）：**样例跑满应跑条数且全部通过**（样例回放）。

"全过"必须有两个维度，缺一都会放行错东西：
1. **去重（防虚增）**：`stage=sample` 必须带非空 `label`，证据按 label 存 `{label: bool}`；
   同一 label 重复记录取逻辑与（曾失败即永久失败），不允许"重试到过"。
   否则同一条样例录两次就会把"只测了 1 条"伪装成"两条全过"。
2. **分母（防覆盖不足/防事后改判据）**：必须先用 `set_expected_samples(n)` 声明**应跑**
   多少条样例；只有 `len(samples_seen) >= n` 且全部为 True 才叫"全过"。
   否则只跑 1 条就恒真——"见过的都过了"不等于"全过"。
   `samples_expected <= 0` 时 reference/brute 一律**不放行**（判据不明即判负）。
   分母只能**在跑样例之前、且只能调高**：跑过样例后再改、或把 5 改成 1，都直接 raise
   （否则"跑 1 条被拒 → 把分母改成 1 → 放行"，分母就形同虚设）。

落库重开（S5/N1）：
- `history` / `finished` / `samples_expected` / `version` 都必须随快照回灌。
  `version` 尤其关键：证据按 `record.version == 当前 version` 回放，
  若重开时把 version 硬编码成 1，**v1 的通过证据会被套到 v2 代码上（fail-open）**。
- `finished=True` 时构造期会用证据复核一遍，复核不过直接 `ValueError("落库数据不自洽…")`；
  `can_finalize()` 的对外语义不变（finished 仍返回 True）。

注意：`can_finalize` 只回答"这个产物本身够不够格提交"，
不作跨产物仲裁（ref 三份一致性 / brute 可疑降级属预生成流水线职责，见设计 §5 ③）。
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Dict, Final, Iterable, List, Mapping, Optional, Tuple

ARTIFACT_GENERATOR: Final[str] = "generator"
ARTIFACT_REF_C: Final[str] = "ref_c"
ARTIFACT_REF_CPP: Final[str] = "ref_cpp"
ARTIFACT_REF_PY: Final[str] = "ref_py"
ARTIFACT_BRUTE: Final[str] = "brute"

#: 每题固定 5 个产物（顺序即流水线顺序：先数据，后参考解，最后暴力解）
ARTIFACT_KINDS: Final[Tuple[str, ...]] = (
    ARTIFACT_GENERATOR,
    ARTIFACT_REF_C,
    ARTIFACT_REF_CPP,
    ARTIFACT_REF_PY,
    ARTIFACT_BRUTE,
)

#: 参考解三种语言，共用同一套放行规则
REFERENCE_KINDS: Final[Tuple[str, ...]] = (
    ARTIFACT_REF_C,
    ARTIFACT_REF_CPP,
    ARTIFACT_REF_PY,
)

# 尝试阶段（record_attempt 的 stage 取值，闭合词表：未知 stage 直接拒绝）
STAGE_COMPILE: Final[str] = "compile"
STAGE_RUN: Final[str] = "run"
STAGE_SAMPLE: Final[str] = "sample"
STAGE_CROSS_CHECK: Final[str] = "cross_check"
STAGE_GRAMMAR: Final[str] = "grammar"

ALLOWED_STAGES: Final[Tuple[str, ...]] = (
    STAGE_COMPILE,
    STAGE_RUN,
    STAGE_SAMPLE,
    STAGE_CROSS_CHECK,
    STAGE_GRAMMAR,
)


def compute_artifact_code_hash(code_text: str) -> str:
    """与 session.state 同格式的代码指纹（本模块自带，避免依赖待删模块）。"""
    raw = str(code_text or "").encode("utf-8", errors="replace")
    return "sha256:" + sha256(raw).hexdigest()


def _require_bool(value: Any, *, field: str) -> bool:
    """`passed` 必须是真 bool；字符串/整数/None 一律拒绝（fail-closed）。"""
    if not isinstance(value, bool):
        raise TypeError(
            f"{field} 必须是 bool，收到 {type(value).__name__}({value!r})；"
            "禁止用真值转换决定产物放行"
        )
    return value


def _require_positive_int(value: Any, *, field: str) -> int:
    """应跑样例数必须是正整数；bool 也算非法（True 会被悄悄当成 1）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} 必须是 int，收到 {type(value).__name__}({value!r})")
    if value <= 0:
        raise ValueError(f"{field} 必须 > 0，收到 {value!r}")
    return int(value)


def _require_non_negative_int(value: Any, *, field: str) -> int:
    """构造期允许 0（= 尚未声明应跑条数），但不允许负数或非 int。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} 必须是 int，收到 {type(value).__name__}({value!r})")
    if value < 0:
        raise ValueError(f"{field} 必须 >= 0，收到 {value!r}")
    return int(value)


@dataclass(frozen=True)
class AttemptRecord:
    """单条尝试记录（不可变，进 history 后不可被外部改回会话状态）。"""

    version: int
    stage: str
    passed: bool
    label: str = ""
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": int(self.version),
            "stage": str(self.stage),
            "passed": bool(self.passed),
            "label": str(self.label),
            "detail": str(self.detail),
        }


class ArtifactSession:
    """单产物会话：一份代码 + 该代码版本上的证据 + 尝试历史。"""

    def __init__(
        self,
        problem_id: Any,
        artifact_kind: str,
        code: str = "",
        attempts: int = 0,
        history: Optional[Iterable[Any]] = None,
        finished: bool = False,
        samples_expected: int = 0,
        version: int = 1,
    ) -> None:
        kind = str(artifact_kind or "").strip().lower()
        if kind not in ARTIFACT_KINDS:
            raise ValueError(
                f"unsupported artifact_kind={artifact_kind!r}; allowed={list(ARTIFACT_KINDS)}"
            )
        self._problem_id = problem_id
        self._artifact_kind = kind
        self._code = str(code or "")
        # 版本必须由快照回灌：硬编码 1 会把 v1 的历史证据错配到 v2 代码上（fail-open）。
        self._version = _require_positive_int(version, field="version")
        # 传入的 history 一律复制为不可变记录，外部后续改动列表/字典不影响本会话。
        self._history: List[AttemptRecord] = [self._normalize_history_item(item) for item in list(history or [])]
        # attempts 未显式给出时以 history 长度为准：落库重开时不必再单独传计数。
        given_attempts = max(0, int(attempts or 0))
        self._attempts = given_attempts or len(self._history)
        self._finished = _require_bool(finished, field="finished")
        # 应跑样例数是"题/产物级"声明，不随换码失效（0 = 尚未声明 = 判负）。
        self._samples_expected = _require_non_negative_int(samples_expected, field="samples_expected")
        self._evidence: Dict[str, Any] = {}
        self._reset_evidence()
        # 版本单调性：记录只会盖当时版本戳，因此 history 里不可能出现"比当前 version 更新"的记录。
        # 出现即说明落库漏存 version（重开会从 1 起，把旧证据错配到新代码上）——必须报错而非放行。
        future_versions = [int(item.version) for item in self._history if int(item.version) > int(self._version)]
        if future_versions:
            raise ValueError(
                f"落库数据不自洽：history 中存在比快照 version={self._version} 更新的记录"
                f"（最高 v{max(future_versions)}），version 字段可能没随快照持久化"
            )
        self._replay_history_evidence()
        # 落库自洽性：finished=True 表示"曾按证据判过通过"，重开时必须能复核出来。
        # 否则 can_finalize 的 finished 短路会把"漏存字段 / 张冠李戴的历史"伪装成已通过。
        if self._finished:
            ok, reason = self._judge_by_evidence()
            if not ok:
                raise ValueError(f"落库数据不自洽：finished=True 但证据不足：{reason}")

    # ── 只读属性（返回副本，避免外部就地改内部状态）────────────────────────

    @property
    def problem_id(self) -> Any:
        return self._problem_id

    @property
    def artifact_kind(self) -> str:
        return self._artifact_kind

    @property
    def code(self) -> str:
        return self._code

    @property
    def version(self) -> int:
        return int(self._version)

    @property
    def attempts(self) -> int:
        return int(self._attempts)

    @property
    def finished(self) -> bool:
        return bool(self._finished)

    @property
    def samples_expected(self) -> int:
        """应跑样例条数（0 = 尚未声明，reference/brute 一律判负）。"""
        return int(self._samples_expected)

    @property
    def code_hash(self) -> str:
        return compute_artifact_code_hash(self._code)

    @property
    def history(self) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in self._history]

    @property
    def evidence(self) -> Dict[str, Any]:
        return self._evidence_copy()

    # ── 唯一的状态写入入口 ──────────────────────────────────────────────────

    def set_code(self, code: str) -> int:
        """替换本产物代码，返回新版本号。

        换码即失效：上一版本的通过证据不再对应当前代码，因此清空证据并撤销 finished。
        内容相同的替换不算新版本（避免无意义地丢失证据）。

        这是**唯一**能撤销 finished 的入口：已 finalize 的产物要重跑必须显式换码，
        不允许在"已完成"状态下继续写入新证据。
        """
        new_code = str(code or "")
        if new_code == self._code:
            return int(self._version)
        self._code = new_code
        self._version += 1
        self._finished = False
        self._reset_evidence()
        return int(self._version)

    def set_expected_samples(self, count: int) -> int:
        """声明"应跑多少条样例"，返回生效值。

        为什么需要：`can_finalize` 的"样例全过"必须有分母。只跑 1 条就恒真属于
        覆盖不足（"见过的都过了"不等于"全过"），所以 reference/brute 在未声明时一律判负。

        规则（防"事后改判据凑通过"），按校验顺序：
        1. 已 finalize → RuntimeError（与 S3 终态语义一致，需先 set_code 重开）。
        2. `count` 必须是 `int` 且 `> 0`：`True`/`"2"`/`2.0` → TypeError，`0`/负数 → ValueError。
        3. **一旦记录了任何 sample，无论调高还是调低都 RuntimeError**（必须走 `set_code`
           重开；`set_code` 会清空 `samples_seen`，之后可重新声明）。
           否则"expected=5 跑 1 条被拒 → 改成 1 → 放行"，分母形同虚设。
        4. **未记录样例时只增不减**：`count < 当前值` → ValueError（降分母等于放宽判据）；
           当前为 0（未声明）时可自由设定。
        5. 已跑条数多于声明值时**不裁剪**（多跑不算错，判据只看"跑满且全过"）。
        """
        if self._finished:
            raise RuntimeError(
                "artifact session 已 finalized，拒绝修改应跑样例数；如需调整请先 set_code 换码"
            )
        target = _require_positive_int(count, field="samples_expected")
        if self._evidence["samples_seen"]:
            raise RuntimeError(
                "已记录样例结果，不能再调整应跑样例数"
                "（否则等于事后改判据：跑 1 条被拒后把分母调成 1 就能放行）"
            )
        current = int(self._samples_expected)
        if target < current:
            raise ValueError(f"应跑样例数只增不减：当前 {current} -> 目标 {target}")
        self._samples_expected = target
        return int(self._samples_expected)

    def record_attempt(self, result: Mapping[str, Any]) -> AttemptRecord:
        """记录一次产物验证尝试，返回不可变记录。

        `result` 契约（未知 stage 直接拒绝、`passed` 必须是真 bool）::

            {"stage": "compile|run|sample|cross_check|grammar",
             "passed": bool,          # 必填语义：缺省按 False，其他类型 raise TypeError
             "label": str,            # stage=sample 时必填（按样例去重）
             "detail": str}           # 可选，失败原因摘要
        """
        if self._finished:
            raise RuntimeError(
                "artifact session 已 finalized，拒绝写入新的尝试记录；"
                "如需重跑请先 set_code 换码（换码会清空证据并撤销 finished）"
            )
        if not isinstance(result, Mapping):
            raise TypeError("record_attempt expects a mapping with 'stage'/'passed'")

        stage = str(result.get("stage") or "").strip().lower()
        if stage not in ALLOWED_STAGES:
            raise ValueError(f"unsupported stage={stage!r}; allowed={list(ALLOWED_STAGES)}")

        label = str(result.get("label") or "").strip()
        if stage == STAGE_SAMPLE and not label:
            raise ValueError(
                "stage=sample 必须提供非空 label：证据按样例 label 去重，"
                "否则同一条样例录两次会被算成两条全过"
            )

        record = AttemptRecord(
            version=int(self._version),
            stage=stage,
            passed=_require_bool(result.get("passed", False), field="passed"),
            label=label,
            detail=str(result.get("detail") or ""),
        )
        self._history.append(record)
        self._attempts += 1
        self._apply_evidence(record)
        return record

    def can_finalize(self) -> Tuple[bool, str]:
        """按产物类型判断是否达到可提交标准，返回 (ok, 拒绝原因)。

        `finished=True` 时直接放行（终态）；"证据是否真的够"由
        `_judge_by_evidence()` 负责，构造期用它做落库自洽复核。
        """
        if self._finished:
            return True, ""
        return self._judge_by_evidence()

    def finalize(self) -> None:
        """标记产物已完成。未达标准时拒绝（不做静默降级）。"""
        ok, reason = self.can_finalize()
        if not ok:
            raise RuntimeError(reason)
        self._finished = True

    def snapshot(self) -> Dict[str, Any]:
        """审计/落库快照（不含 history 全文，history 单独取）。"""
        return {
            "problem_id": self._problem_id,
            "artifact_kind": self._artifact_kind,
            "version": int(self._version),
            "code_hash": self.code_hash,
            "attempts": int(self._attempts),
            "finished": bool(self._finished),
            "samples_expected": int(self._samples_expected),
            "evidence": self._evidence_copy(),
        }

    # ── 内部实现 ────────────────────────────────────────────────────────────

    def _evidence_copy(self) -> Dict[str, Any]:
        """证据的独立副本（samples_seen 是内层 dict，必须一并复制，防外部就地改）。"""
        copied = dict(self._evidence)
        copied["samples_seen"] = dict(self._evidence.get("samples_seen") or {})
        copied["samples_expected"] = int(self._samples_expected)
        return copied

    def _reset_evidence(self) -> None:
        self._evidence = {
            "compile_ok": False,
            "run_ok": False,
            "grammar_ok": False,
            "samples_seen": {},
            "cross_check_recorded": False,
            "cross_check_passed": False,
            "cross_check_failed": False,
        }

    def _replay_history_evidence(self) -> None:
        """按 history 顺序重放属于当前版本的证据（P3 落库重开时保持已通过状态）。

        只回放 `record.version == 当前版本` 的记录：更早版本的证据不对应当前代码。
        """
        for record in self._history:
            if int(record.version) == int(self._version):
                self._apply_evidence(record)

    def _apply_evidence(self, record: AttemptRecord) -> None:
        if record.stage == STAGE_COMPILE:
            self._evidence["compile_ok"] = bool(record.passed)
        elif record.stage == STAGE_RUN:
            self._evidence["run_ok"] = bool(record.passed)
        elif record.stage == STAGE_GRAMMAR:
            self._evidence["grammar_ok"] = bool(record.passed)
        elif record.stage == STAGE_SAMPLE:
            # 按 label 去重 + fail-closed：同一 label 曾失败则永久失败（不允许重试到过）。
            seen = dict(self._evidence["samples_seen"])
            previous = seen.get(record.label)
            current = bool(record.passed)
            seen[record.label] = current if previous is None else bool(previous and current)
            self._evidence["samples_seen"] = seen
        elif record.stage == STAGE_CROSS_CHECK:
            self._evidence["cross_check_recorded"] = True
            # 对拍不一致是确定性证据（不是 flaky 超时），一旦出现即"下毒"：
            # 同一代码版本内后续单次通过不能翻案；只有 set_code 换码才会清空。
            if record.passed:
                if not self._evidence["cross_check_failed"]:
                    self._evidence["cross_check_passed"] = True
            else:
                self._evidence["cross_check_failed"] = True
                self._evidence["cross_check_passed"] = False

    def _sample_summary(self) -> Tuple[int, int]:
        """返回 (去重后已跑的样例数, 其中通过的样例数)。"""
        seen = dict(self._evidence["samples_seen"])
        return len(seen), sum(1 for passed in seen.values() if passed)

    def _judge_by_evidence(self) -> Tuple[bool, str]:
        """纯证据判定（忽略 finished 短路）——供 can_finalize 与落库自洽复核共用。"""
        if self._artifact_kind == ARTIFACT_GENERATOR:
            return self._can_finalize_generator()
        if self._artifact_kind in REFERENCE_KINDS:
            return self._can_finalize_reference()
        return self._can_finalize_brute()

    def _sample_coverage_problem(self) -> str:
        """样例覆盖判定：未声明分母 / 未跑满 都返回拒绝原因，否则返回空串。"""
        expected = int(self._samples_expected)
        total, _ = self._sample_summary()
        if expected <= 0:
            return (
                f"{self._artifact_kind} 未声明应跑样例数（samples_expected），"
                '无法判定"全过"'
            )
        if total < expected:
            return f"{self._artifact_kind} 样例未跑全：已跑 {total} / 应跑 {expected}"
        return ""

    def _can_finalize_generator(self) -> Tuple[bool, str]:
        missing: List[str] = []
        if not self._evidence["run_ok"]:
            missing.append("generator 未跑通：缺少通过的执行记录（stage=run）")
        if not self._evidence["grammar_ok"]:
            missing.append("generator 未通过 grammar 校验（stage=grammar）")
        return self._verdict(missing)

    def _can_finalize_reference(self) -> Tuple[bool, str]:
        missing: List[str] = []
        total, passed = self._sample_summary()
        coverage = self._sample_coverage_problem()
        if coverage:
            missing.append(coverage)
        elif passed != total:
            missing.append(f"{self._artifact_kind} 样例未全过：{passed}/{total}")
        if not self._evidence["cross_check_recorded"]:
            missing.append(f"{self._artifact_kind} 缺少对拍记录（stage=cross_check）")
        elif not self._evidence["cross_check_passed"]:
            missing.append(f"{self._artifact_kind} 对拍未通过（存在输出不一致）")
        return self._verdict(missing)

    def _can_finalize_brute(self) -> Tuple[bool, str]:
        missing: List[str] = []
        total, passed = self._sample_summary()
        coverage = self._sample_coverage_problem()
        if coverage:
            missing.append(coverage)
        elif passed != total:
            missing.append(f"brute 样例未全过：{passed}/{total}")
        return self._verdict(missing)

    def _verdict(self, missing: List[str]) -> Tuple[bool, str]:
        if missing:
            return False, "\n".join(missing)
        return True, ""

    @staticmethod
    def _normalize_history_item(item: Any) -> AttemptRecord:
        if isinstance(item, AttemptRecord):
            _require_bool(item.passed, field="history passed")
            return item
        if isinstance(item, Mapping):
            stage = str(item.get("stage") or "").strip().lower()
            if stage not in ALLOWED_STAGES:
                raise ValueError(f"unsupported history stage={stage!r}")
            label = str(item.get("label") or "").strip()
            if stage == STAGE_SAMPLE and not label:
                raise ValueError("history 中 stage=sample 的记录必须带非空 label")
            return AttemptRecord(
                version=int(item.get("version") or 1),
                stage=stage,
                passed=_require_bool(item.get("passed", False), field="history passed"),
                label=label,
                detail=str(item.get("detail") or ""),
            )
        raise TypeError("history items must be AttemptRecord or mapping")


def open_problem_sessions(problem_id: Any, samples_expected: int = 0) -> Dict[str, ArtifactSession]:
    """为一题开满 5 个产物会话（彼此完全独立）。

    `samples_expected`（应跑样例条数）可一次下发给 5 个会话，避免调用方漏设；
    它不随换码失效（属"题/产物级"声明），0 表示尚未声明。
    """
    return {
        kind: ArtifactSession(
            problem_id=problem_id,
            artifact_kind=kind,
            samples_expected=samples_expected,
        )
        for kind in ARTIFACT_KINDS
    }
