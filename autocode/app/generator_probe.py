"""生成器（测试数据脚本）的准入探针。

在把 generator.py 落库之前跑真进程验一遍：五档都能产出合法输入，
且 `edge` 档必须对 seed 敏感。

最后这条是 2026-11-19 实测 #18 模板展开 补的：那道题的生成器写了
`if profile == "edge": out = ["3 v0"]; return out`，7 次调用给出一模一样的
边界数据，本地按内容去重后边界组只剩 1 条，非样例数据 8 条 < 10，整题在
产物门（artifacts）被回滚 —— 白跑十余分钟。写死常量的错误属于"提示词没
说清 + 没有可验证护栏"，两边都要补：模板里写明 edge 要按 seed 轮转，
这里则用两个 seed 对拍，不一致才放行。

放在独立模块而不是 PrepService 里，是因为 prep_service.py 已顶到 800 行硬线。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from ..engine.datagen import grammar as grammar_engine
from ..engine.datagen import input_parser
from ..engine.runner import VERDICT_AC, run_case
from .prep_models import GENERATOR_TIMEOUT_MS

# 探针要覆盖的五档（edge 跑两次，用来对拍 seed 敏感性）
PROBES: Tuple[Tuple[str, int], ...] = (
    ("tiny", 1),
    ("edge", 2),
    ("edge", 5),
    ("max", 3),
    ("small", 4),
)


def probe(spec: Dict[str, Any], code: str) -> Tuple[bool, str]:
    """返回 (是否合格, 说明)。不合格时说明会作为反馈回灌给模型去改。"""
    if not code.strip():
        return False, "返回内容里没有代码"
    checks = []
    produced: Dict[str, str] = {}
    for profile, seed in PROBES:
        result = run_case("python", code, "", GENERATOR_TIMEOUT_MS, args=(str(seed), profile))
        verdict = str(result.get("verdict"))
        if verdict != VERDICT_AC:
            return False, (
                f"profile={profile} seed={seed} 执行失败 verdict={verdict}\n"
                f"stderr={str(result.get('stderr') or '')[:800]}"
            )
        text = str(result.get("stdout") or "")
        if not text.strip():
            return False, f"profile={profile} 没有输出任何数据"
        try:
            parsed = input_parser.parse_input(spec, text)
        except grammar_engine.GrammarError as exc:
            return False, f"profile={profile} 输出不符合 input_grammar：{exc.render()}"
        violations = input_parser.validate_input(spec, parsed)
        if violations:
            return False, f"profile={profile} 校验失败：{violations[0]}"
        produced[f"{profile}/{seed}"] = text
        checks.append(f"{profile}{seed}:{len(text)}B")
    if produced["edge/2"] == produced["edge/5"]:
        return False, (
            "edge 档对 seed 不敏感：seed=2 和 seed=5 的输出逐字节相同。"
            "edge 必须按 seed 轮转不同边界情形（最小规模 / 全相同 / 全取下界 / 单点取上界 / "
            "退化结构 / 并列），同一种情形也要用 seed 换具体数值。"
            "写死常量的边界组去重后只剩 1 条，整题会因数据不足作废。"
        )
    return True, "profiles=" + ",".join(checks)
