"""D1 切题：模型只给行号，题面由程序从原文切片。

这是与 v1 最大的差别。v1 让模型复述题面，`10^9` 被写成 `109` 也无人察觉；
v2 的契约里没有题面字段，模型想改也没有地方写。

送模型前先删页眉/页脚/页码：这些行不属于题面，留着会跟着行号一起被切进去。
切完不直接入库：先落 `import_segments`，人工勾选确认后才写 `problems`。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..app.ingest_csp import parse_samples
from . import banks, config, problem_purge
from . import difficulty as difficulty_mod
from .llm_stage import StageRunner
from .stages import SEGMENT
from .validators import check_segments, judge_supported

#: 整份输入的下限：低于这个长度不可能有任何题目，用于快速失败，省掉一次模型调用。
#: 注意**只作用于整份输入**，不用于单道题——判断"这一段算不算一道题"是模型的活，
#: 代码不该用字数替它做决定（曾经用 100 字卡单题，把 95 字的「两数之和」静默丢掉了）。
MIN_STATEMENT_CHARS = 100

#: 题干标题行：`宝藏（treasure）`。整行只有"名字 + 括号里的 slug"，没有空格。
#: 页眉的 `第32 次认证宝藏（treasure）` 因为名字里有空格，不会被误认。
_HEADING_RE = re.compile(r"^([^（）()\s]{1,40})[（(]([a-z0-9][a-z0-9_-]{0,30})[）)]$")
#: 模型给的起点偏离标题行超过这么多行就不吸附——宁可不动，也不猜。
SNAP_WINDOW = 45
_SLUG_IN_TITLE_RE = re.compile(r"[（(]([a-z0-9][a-z0-9_-]{0,30})[）)]\s*$")


def _title_slug(title: Any) -> str:
    match = _SLUG_IN_TITLE_RE.search(str(title or "").strip())
    return match.group(1) if match else ""


def snap_to_headings(items: List[Dict[str, Any]], lines: Sequence[str],
                     window: int = SNAP_WINDOW) -> int:
    """把每道题的起点吸附到题干标题行，并用新起点重算上一题的结束行。

    为什么要做：模型对"这道题从哪一行开始"经常差几十行（实测 CSP32 把「宝藏」
    提前到上一题的解释段里，连带 37 行正文被算进下一题）。行号一偏，图就会按
    页码挂到错的题上——题是对的，图是别人的，比缺图更难发现。

    标题行 `宝藏（treasure）` 是代码能确定性认出来的唯一锚点，所以只信它：
    slug 取自模型自己给的标题，距离超过 window 或吸附后不再递增，一律保留原值。
    返回吸附成功的题数。
    """
    index: Dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line.strip())
        if match and match.group(2) not in index:
            index[match.group(2)] = number
    if not index:
        return 0

    starts: List[int] = []
    for item in items:
        current = int(item["start_line"])
        target = index.get(_title_slug(item.get("title")))
        usable = target is not None and target != current and abs(target - current) <= window
        starts.append(target if usable else current)

    # 吸附后必须仍然严格递增，否则说明锚点比模型给的还乱，整批放弃
    if any(right <= left for left, right in zip(starts, starts[1:])):
        return 0

    snapped = sum(1 for item, start in zip(items, starts) if start != int(item["start_line"]))
    if not snapped:
        return 0

    for position, (item, start) in enumerate(zip(items, starts)):
        item["start_line"] = start
        if position + 1 < len(starts):
            item["end_line"] = starts[position + 1] - 1
        elif int(item["end_line"]) < start:
            item["end_line"] = len(lines)
    return snapped


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(raw: str) -> str:
    text = str(raw or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


#: 下标字符表：变量下标常用的字母数字。表里没有的字符回退保留原写法。
_SUBSCRIPT: Dict[str, str] = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌",
    "(": "₍", ")": "₎", "n": "ₙ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ", "a": "ₐ",
    "e": "ₑ", "o": "ₒ", "x": "ₓ", "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ",
    "p": "ₚ", "h": "ₕ", "r": "ᵣ", "l": "ₗ",
}
_SUPERScript: Dict[str, str] = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼",
    "(": "⁽", ")": "⁾",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ", "v": "ᵛ",
    "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
}
#: 代码风记号 → 课本风记号（导入时统一，学生看到的题面不出现 d_i / 1 <= n <= 1e5）
_SUB_RE = re.compile(r"\b([A-Za-z])_\{([^}]+)\}")
_SUB_BARE_RE = re.compile(r"\b([A-Za-z])_([0-9ijnkvx]+)\b")
_E_RE = re.compile(r"\b(\d+)e(\d+)\b")


def _to_subscript(chars: str) -> Optional[str]:
    out = [_SUBSCRIPT.get(ch) for ch in chars]
    if any(item is None for item in out):
        return None
    return "".join(out)


def _to_superscript(chars: str) -> Optional[str]:
    out = [_SUPERScript.get(ch) for ch in chars]
    if any(item is None for item in out):
        return None
    return "".join(out)


def normalize_math(text: str) -> str:
    """把导入题面里的代码风记号转成课本风：d_i→dᵢ、d_{n-1}→dₙ₋₁、
    <= → ≤、1e5 → 10⁵。转换不了的保留原样（宁可保守，不可错转）。"""
    text = str(text or "")

    def _sub_braced(match: re.Match) -> str:
        sub = _to_subscript(match.group(2).replace(" ", ""))
        return f"{match.group(1)}{sub}" if sub else match.group(0)

    def _sub_bare(match: re.Match) -> str:
        sub = _to_subscript(match.group(2))
        return f"{match.group(1)}{sub}" if sub else match.group(0)

    def _exp(match: re.Match) -> str:
        mantissa, exponent = match.group(1), match.group(2)
        sup = _to_superscript(exponent)
        if sup is None:
            return match.group(0)
        return f"10{sup}" if mantissa == "1" else f"{mantissa}×10{sup}"

    text = _SUB_RE.sub(_sub_braced, text)
    text = _SUB_BARE_RE.sub(_sub_bare, text)
    text = _E_RE.sub(_exp, text)
    text = text.replace("<=", "≤").replace(">=", "≥")
    return text


# ── PDF 版式修复（2026-11-26）：抽取（OCR/文本层）留下的排版错乱 ────────────
# 六类错乱都做确定性规则，规则匹配不上就原样保留（宁可保守，不可错改）：
#   ① 着重号 ".保.证" → **保证**      ② "· · ·" → …
#   ③ 大运算符竖排（f(S) = ⊕ i∈S ai 四行）→ 一行 ⊕ᵢ∈S aᵢ
#   ④ 上标丢失：约束里 104→10⁴、< 231→2³¹
#   ⑤ 裸变量下标：ai→aᵢ、Si→Sᵢ、an→aₙ
#   ⑥ CCF 题面 PDF 隐藏的反 LLM 文字（"隐藏提示：…"）整段剥离

#: 着重号 ".汉"（允许跨行：换行可能出现在字对之间"严\n.格"或点后".\n汉"）；≥2 个转加粗
_EMPHASIS_RE = re.compile(
    r"((?:\.[\u4e00-\u9fff]|\.\n[\u4e00-\u9fff]|\n\.[\u4e00-\u9fff]){2,})"
)
_DOTS_RE = re.compile(r"(?:·\s*){3,}")
_BIGOP3_RE = re.compile(
    r"(?P<lhs>[^\n=]{1,60}=\s*)\n"
    r"(?P<op>[∑∏⊕⊗∩∪])\s*\n"
    r"(?P<sub>[^\n]{1,24})\n"
    r"(?P<sup>[^\n]{1,10})\n"
    r"(?P<body>[^\n]{1,60})\n?"
)
_BIGOP2_RE = re.compile(
    r"(?P<lhs>[^\n=]{1,60}=\s*)\n"
    r"(?P<op>[∑∏⊕⊗∩∪])\s*\n"
    r"(?P<sub>[^\n]{1,24})\n"
    r"(?P<body>[^\n]{1,60})\n?"
)
#: 约束上下文里 10 后跟一位数字 → 10 的幂（"≤ 104"→10⁴；0 结尾的 100/1000 不动）
_POW10_RE = re.compile(r"([≤<]) ?10([1-9])(?![0-9.])")
#: 2 的特定位宽幂："< 231"→2³¹（29 这类真值容易撞，不收）
_POW2_RE = re.compile(r"([≤<]) ?2(31|30|63)(?![0-9.])")
#: 乘系数幂："≤5×105"→5×10⁵（末位后不跟数字/小数点，101 这类真值不撞）
_TIMES_POW_RE = re.compile(r"×10([1-9])(?![0-9.])")
#: 值域区间幂："[0, 103]"→[0, 10³]（值域语境里 103 几乎必是 10³）
_BRACKET_POW_RE = re.compile(r"\[0, ?10([1-9])\]")
#: ∑ 上下限竖排拆行："…∑n\ni=1 mᵢ ≤5×105…" → "…∑ᵢ₌₁ⁿ mᵢ ≤5×10⁵…"（合并后补空行守恒）
_SUM_SPLIT_RE = re.compile(r"∑(?P<up>[^\s\n])\n(?P<lo>[ijk])=1 (?P<rest>[^\n]*)")
#: 裸两字符变量：ai→aᵢ、an→aₙ、a1→a₁。三字母以上的词内部被"前字符是字母"挡住；
#: in/on/at 等英文虚词列入黑名单（an 例外——中文题面里独立 an 几乎必是数列 aₙ）
_VAR_SUB_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z])([0-9in])(?![0-9A-Za-z])")
_VAR_SUB_BLOCK = {"in", "on", "at", "is", "it", "no", "do", "go", "to", "of", "if", "as", "so", "up"}
_HIDDEN_HINT_RE = re.compile(r"隐藏提示[:：].*?(?=【|$)", re.DOTALL)
#: 题面里的样例块：【样例1 输入】…到下一个【标题】或文末
_SAMPLE_BLOCK_RE = re.compile(r"(【样例[^\n】]*】\n)(.*?)(?=【|$)", re.DOTALL)


def _strip_sample_block(match: re.Match) -> str:
    header, body = match.group(1), match.group(2)
    stripped = strip_sample_line_numbers(body)
    removed = max(0, body.count("\n") - stripped.count("\n"))
    return header + stripped + "\n" * removed


def _sub_op_chars(chars: str) -> str:
    """竖排下标行转 Unicode 下标：能转的转（i→ᵢ、=→₌、1→₁），转不了的保留。"""
    return "".join(_SUBSCRIPT.get(ch, ch) for ch in chars.strip())


def _bigop_sub(match: re.Match) -> str:
    lhs = match.group("lhs").strip().rstrip("=").strip().replace("\n", " ")
    op = match.group("op")
    sub = _sub_op_chars(match.group("sub"))
    sup = None
    if "sup" in match.groupdict():
        sup_raw = match.group("sup").strip()
        sup = _to_superscript(sup_raw) if sup_raw else None
        # "上标行"是字母+数字（ai / a1 / an）→ 那其实是公式主体被错位成四行，
        # 交给两行形态处理，绝不能把它当下标转成 ᵃⁱ
        if sup is None or re.fullmatch(r"[A-Za-z][0-9in]", sup_raw):
            return match.group(0)
    body = match.group("body").strip()
    body = _VAR_SUB_RE.sub(lambda m: m.group(1) + (_SUBSCRIPT.get(m.group(2), m.group(2))), body)
    op_text = op + (sub if sub else "")
    if sup:
        op_text += sup
    # 竖排 N 行合成 1 行，行数不能少：layout 分块与插图 after_line 都按行号定位，
    # 差的行用空行补齐，题面行号完全不变
    return f"{lhs} = {op_text} {body}\n" + "\n" * (match.group(0).count("\n") - 1)


def repair_pdf_layout(text: str) -> str:
    """PDF 抽取题面的版式修复。每条规则都只在精确匹配时生效，失败原样保留。"""
    text = str(text or "")
    # ⑥ 反 LLM 隐藏文字（不属于题目内容，留着还会诱导学生侧代码生成）
    text = _HIDDEN_HINT_RE.sub("", text)
    # 约束里的 "1 <= n <= 104"：先把 <= 转成 ≤（幂等，normalize_math 尾部还会做一遍），
    # 否则上标恢复的"紧邻 ≤"前提匹配不到
    text = text.replace("<=", "≤").replace(">=", "≥")
    # ⓪ 题面里的样例块剥行号（CSP 官方样例带行号，抽取后混进数据展示）；
    #   剥掉的行用空行补齐，行数守恒
    text = _SAMPLE_BLOCK_RE.sub(_strip_sample_block, text)
    # ① 着重号：连续 ≥2 个 ".汉" 只去点留字（曾试过转 **加粗**，但前端按行渲染
    #   纯文本、跨行的加粗对配不上，全成了字面星号，用户明确反馈过）；剩余散点只删点
    text = _EMPHASIS_RE.sub(lambda m: m.group(1).replace(".", ""), text)
    text = re.sub(r"\.(?=[\u4e00-\u9fff])", "", text)
    # ② 散列间隔点
    text = _DOTS_RE.sub("…", text)
    # ③ 大运算符竖排：先试带下标+上标的三行形态（∑/i=1/n/ai），再退两行形态
    text = _BIGOP3_RE.sub(_bigop_sub, text)
    text = _BIGOP2_RE.sub(_bigop_sub, text)
    # ④⑤ 裸变量下标与丢失的上标（先转下标：aᵢ < 2³¹ 的 2³¹ 依赖 aᵢ 已就位）
    def _var_sub(match: re.Match) -> str:
        if match.group(0) in _VAR_SUB_BLOCK:
            return match.group(0)
        sub = _SUBSCRIPT.get(match.group(2))
        return match.group(1) + sub if sub else match.group(0)

    text = _VAR_SUB_RE.sub(_var_sub, text)

    def _pow10(match: re.Match) -> str:
        sup = _to_superscript(match.group(2))
        return f"{match.group(1)} 10{sup}" if sup else match.group(0)

    def _pow2(match: re.Match) -> str:
        sup = _to_superscript(match.group(2))
        return f"{match.group(1)} 2{sup}" if sup else match.group(0)

    text = _POW10_RE.sub(_pow10, text)
    text = _POW2_RE.sub(_pow2, text)

    def _times_pow(match: re.Match) -> str:
        sup = _to_superscript(match.group(1))
        return f"×10{sup}" if sup else match.group(0)

    def _bracket_pow(match: re.Match) -> str:
        sup = _to_superscript(match.group(1))
        return f"[0, 10{sup}]" if sup else match.group(0)

    text = _TIMES_POW_RE.sub(_times_pow, text)
    text = _BRACKET_POW_RE.sub(_bracket_pow, text)

    def _sum_split(match: re.Match) -> str:
        # 上标转不出来（大写字母等）就不合并，宁缺毋错
        up = _to_superscript(match.group("up"))
        if not up:
            return match.group(0)
        lo = _sub_op_chars(match.group("lo") + "=1")
        # 合并成一行后补一个空行，行号完全不变（layout/插图都按行号定位）
        return f"∑{lo}{up} {match.group('rest')}\n"

    text = _SUM_SPLIT_RE.sub(_sum_split, text)
    return text


def normalize_statement(text: str) -> str:
    """导入落库/还原原文的统一入口：先修版式，再转课本风记号。"""
    return normalize_math(repair_pdf_layout(text))


def adapt_statement_keep_lines(statement: str) -> str:
    """切题阶段的题面适配：与 normalize_statement 同一套规则，但保证行数不减。

    模型给的行号分块（layout）按行号定位，行数一变分块就错位。行数变少时在
    末尾补空行（隐藏提示剥除等规则只会动结尾）；行数反而变多属未知情况，保守
    回退原文，宁可不适配也不让排版错位。
    """
    adapted = normalize_statement(statement)
    before = statement.count("\n")
    after = adapted.count("\n")
    if after == before:
        return adapted
    if after < before:
        return adapted + "\n" * (before - after)
    return statement


def strip_sample_line_numbers(text: str) -> str:
    """剥除样例输入/输出里混进来的行号。

    CSP 官方题面的样例带行号（左侧灰色小数字），PDF 抽取后变成两种形态：
      变体 A：行号独立成行，与内容行交替（"1\\n5 3\\n2\\n1 2 3 0 4…"）
      变体 B：行号与内容同行（"1  5 3"）
    只有整块构成严格 1..k 序列且覆盖所有非空行时才剥，否则原样返回——
    输入数据本身就可能以数字开头，绝不能"看起来像就剥"。
    """
    lines = str(text or "").split("\n")
    # 变体 A：行号行与内容行严格交替
    seq: List[str] = []
    expected = 1
    i, n, consumed = 0, len(lines), 0
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s == str(expected) and i + 1 < n and lines[i + 1].strip():
            seq.append(lines[i + 1])
            expected += 1
            consumed += 2
            i += 2
            continue
        consumed += 1
        i += 1
        break
    if expected - 1 >= 3 and consumed == sum(1 for ln in lines if ln.strip()):
        return "\n".join(seq)
    # 变体 B：每行首 token 恰为 1..k
    parts = [ln.split() for ln in lines if ln.strip()]
    if len(parts) >= 3 and all(
        tokens and tokens[0] == str(idx) for idx, tokens in enumerate(parts, start=1)
    ):
        return "\n".join(" ".join(tokens[1:]) for tokens in parts)
    return text


#: 页眉/页脚/页码：整行匹配才删，避免误伤正文里提到"第 3 页"的句子。
_PAGE_FURNITURE_RE = re.compile(
    r"^(?:"
    r"第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?"
    r"|共\s*\d+\s*页"
    r"|CCF\s*CSP.*"
    r"|第\s*\d+\s*次认证.*"
    r"|<!--\s*Page\s*\d+\s*-->"
    r")$"
)


def is_page_furniture(line: str) -> bool:
    """这行是不是页眉/页脚/页码（整行匹配才算）。"""
    return bool(_PAGE_FURNITURE_RE.match(str(line).strip()))


def strip_page_furniture(lines: Sequence[str]) -> Tuple[List[str], List[str]]:
    """删掉整行的页眉/页脚/页码，返回 (保留行, 删除行)。

    只删整行，保留空行和原始缩进：题面由程序逐字切片，不能顺手 strip 每一行。
    """
    kept: List[str] = []
    removed: List[str] = []
    for line in lines:
        if is_page_furniture(line):
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def number_lines(lines: Sequence[str]) -> str:
    width = max(4, len(str(len(lines))))
    return "\n".join(f"{index:0{width}d}| {line}" for index, line in enumerate(lines, start=1))


def slice_lines(lines: Sequence[str], start: int, end: int) -> str:
    """行号是 1-based 闭区间；越界部分自动裁掉。"""
    left = max(0, int(start) - 1)
    right = min(len(lines), int(end))
    return "\n".join(lines[left:right]).strip("\n")


def extract_samples(statement: str) -> List[Dict[str, Any]]:
    try:
        samples, _external, _warnings = parse_samples(statement.split("\n"))
    except Exception:  # noqa: BLE001 - 解析失败不该阻断导入，样例留空后续拒生成
        return []
    out: List[Dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        if not sample.complete:
            continue
        out.append(
            {
                "index": index,
                # CSP 官方题面样例带行号，抽取后行号会混进数据里，导入前剥掉
                "input": strip_sample_line_numbers(sample.input_text),
                "output": strip_sample_line_numbers(sample.output_text),
            }
        )
    return out


def _strip_packed_samples(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对已抽取好的样例逐条剥行号（confirm_import 落库前的兜底，幂等）。"""
    cleaned: List[Dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, dict) or "input" not in sample:
            cleaned.append(sample)
            continue
        cleaned.append(
            {
                **sample,
                "input": strip_sample_line_numbers(str(sample.get("input") or "")),
                "output": strip_sample_line_numbers(str(sample.get("output") or "")),
            }
        )
    return cleaned


def refresh_samples(db: Any, problem: Any) -> List[Dict[str, Any]]:
    """题面样例为空时，按**当前**解析规则从 statement_md 重算一次并落库。

    为什么必须补这一步：`samples_json` 是导入那一刻算出来存下的。解析规则升级后，
    老题仍然带着旧结果（0 组）；而重新导入会被 `statement_hash` 判成重复题、
    只返回旧 problem_id，样例不会更新——用户看到的就是"改完了还是不行"。

    只在为空时触发：已有样例的题不动，避免新规则把人工确认过的结果悄悄改掉。
    """
    statement = str(problem["statement_md"] or "")
    samples = extract_samples(statement)
    if not samples:
        return []
    with db.transaction() as conn:
        conn.execute(
            "UPDATE problems SET samples_json=?, updated_at=? WHERE id=?",
            (_json(samples), _now(), int(problem["id"])),
        )
    return samples


def attach_figures_by_page(
    rows: List[Dict[str, Any]],
    figures: Sequence[Dict[str, Any]],
    page_ranges: Sequence[Sequence[int]],
) -> List[str]:
    """按几何位置把 PDF 图块分给题目：不调模型，只看行号区间。

    规则：图在第 P 页 → 该页的行号区间是 `page_ranges[P-1]`，
    分给"题目行号区间与该页交集最大"的那道题。CSP 试卷基本一题一页，
    交集就是整页；一页多题时取交集最大者（正文落在哪页，图就属于那道题）。

    同一页的多张图会在 `after_line` 上依次 +1，避免全挤在同一行。
    返回告警（没有题目覆盖的页、图块被跳过）。
    """
    warnings: List[str] = []
    if not figures or not page_ranges:
        return warnings
    used: Dict[int, int] = {}
    for figure in figures:
        figure_id = str(figure.get("id") or "")
        try:
            page = int(figure.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= page <= len(page_ranges)):
            continue
        page_start = int(page_ranges[page - 1][0])
        page_end = int(page_ranges[page - 1][1])
        best_row: Optional[Dict[str, Any]] = None
        best_overlap = 0
        for row in rows:
            start, end = int(row["start_line"]), int(row["end_line"])
            overlap = min(page_end, end) - max(page_start, start) + 1
            if overlap > best_overlap:
                best_row, best_overlap = row, overlap
        if best_row is None:
            warnings.append(
                f"{figure_id} 在第{page}页（行{page_start}-{page_end}），"
                "没有题目覆盖这一页，已跳过"
            )
            continue
        start = int(best_row["start_line"])
        # 优先用"图上方的文字行"算插入位置：只按页码会把图一律放到页首，
        # 实测 CSP32 第 6 页的树图画在下半页，按页码得到的是题面第 1 行。
        anchor = int(figure.get("anchor_line") or 0)
        total = len(str(best_row.get("statement_md") or "").split("\n"))
        if anchor > 0:
            after = anchor - start + 1
            after = max(1, min(after, max(1, total)))
        else:
            # 图在本页最上方（上方只有页眉）：应插在"本页第一行之前"，
            # 而不是本页第一行之后——后者会把图压到图内文字下面
            after = max(1, max(start, page_start) - start)
        key = start
        # 同一题的多张图必须严格递增，否则前端排序不稳定、图会挤在一起
        previous = used.get(key, 0)
        if previous:
            after = max(after, previous + 1)
        used[key] = after
        best_row.setdefault("figures", []).append(
            {
                "id": figure_id,
                "page": page,
                "bbox": figure.get("bbox") or [],
                "width": float(figure.get("width") or 0),
                "height": float(figure.get("height") or 0),
                "after_line": after,
            }
        )
    return warnings


def normalize_layout(blocks: List[Dict[str, Any]], lines: Sequence[str]) -> Tuple[List[Dict[str, Any]], int]:
    """把模型给的块整理成"没有裂缝、也不过分碎"的样子。

    两件事，都是模型做不到、只能由代码保证的：

    1. **补缝**：模型偶尔漏标几行（实测 CSP32 有题漏 16 行）。漏掉的行不属于任何块，
       前端就只能猜；这里按正文补成 `para`，顺序不变、文字不丢。
    2. **合并**：模型也常把一段话拆成十几块（实测 192 行给了 130 块），
       相邻的 `para` 合并回一段，否则渲染出来是一堆各自成段的碎句。

    返回 (整理后的块, 补过的行数)。
    """
    total = len(lines)
    filled: List[Dict[str, Any]] = []
    inserted = 0
    cursor = 1

    for block in blocks:
        start = int(block["from"])
        if start > cursor:
            # 第 1 行是题名，标题栏已经有了，不能又当正文补一遍
            gap_start = max(cursor, 2)
            if gap_start < start and any(
                str(lines[number - 1]).strip() for number in range(gap_start, start)
            ):
                filled.append({"kind": "para", "from": gap_start, "to": start - 1})
                inserted += start - gap_start
        filled.append(dict(block))
        cursor = max(cursor, int(block["to"]) + 1)

    if cursor <= total and any(str(lines[number - 1]).strip() for number in range(cursor, total + 1)):
        filled.append({"kind": "para", "from": cursor, "to": total})
        inserted += total - cursor + 1

    merged: List[Dict[str, Any]] = []
    for block in filled:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and block["kind"] == "para"
            and previous["kind"] == "para"
            and int(previous["to"]) + 1 == int(block["from"])
        ):
            previous["to"] = block["to"]
            continue
        merged.append(block)
    return merged, inserted


def _slice_statement(item: Dict[str, Any], lines: Sequence[str]) -> Tuple[List[str], Dict[int, int], int]:
    """切出题面正文：去掉模型标为"不属于题面"的行与页眉页脚。

    返回 `(正文行, 旧相对行号→新相对行号, 删掉的行数)`。映射给分块用：
    删行之后块坐标必须跟着挪，否则排版会把正文挂到别的小节下面。
    """
    start, end = int(item["start_line"]), int(item["end_line"])
    skip = set(item.pop("skip_lines", None) or [])
    body: List[str] = []
    mapping: Dict[int, int] = {}
    removed = 0
    for absolute in range(start, end + 1):
        text = lines[absolute - 1]
        if absolute in skip or is_page_furniture(text):
            removed += 1
            continue
        mapping[absolute - start + 1] = len(body) + 1
        body.append(text)
    return body, mapping, removed


def _remap_blocks(blocks: Any, mapping: Dict[int, int]) -> Optional[List[Dict[str, Any]]]:
    """把分块坐标换到删行后的题面上；整块都被删掉就丢掉这块。"""
    out: List[Dict[str, Any]] = []
    for block in blocks or []:
        hits = [
            mapping[number]
            for number in range(int(block["from"]), int(block["to"]) + 1)
            if number in mapping
        ]
        if hits:
            out.append({**block, "from": min(hits), "to": max(hits)})
    return out or None


def segment_text(
    db: Any,
    runner: StageRunner,
    text: str,
    *,
    source_name: str = "",
    kind: str = "text",
    figures: Optional[Sequence[Dict[str, Any]]] = None,
    page_ranges: Optional[Sequence[Sequence[int]]] = None,
    pdf_key: str = "",
    bank_id: int = banks.DEFAULT_ID,
    role: str = "student",
) -> Dict[str, Any]:
    # 先判库和权限，再花模型的钱：学生选了永久库就直接回去，不白烧一次调用。
    bank = banks.get(db, bank_id)
    if bank is None:
        return {"error": "题库不存在", "code": "NOT_FOUND"}
    if not banks.can_manage(bank, role):
        return {"error": banks.DENY_ADD, "code": "FORBIDDEN"}
    cleaned = clean_text(text)
    if len(cleaned) < MIN_STATEMENT_CHARS:
        return {"error": f"文本太短（{len(cleaned)} 字符），至少 {MIN_STATEMENT_CHARS} 字符", "code": "INVALID_INPUT"}
    if len(cleaned) > config.MAX_IMPORT_CHARS:
        return {
            "error": f"文本超过 {config.MAX_IMPORT_CHARS} 字符，请拆分后分批导入",
            "code": "PAYLOAD_TOO_LARGE",
        }

    lines, removed = strip_page_furniture(cleaned.split("\n"))
    # 行号与 raw_text 必须基于同一份文本：清洗后重新拼一次，避免"预览是脏的、切的是干净的"
    cleaned = clean_text("\n".join(lines))
    lines = cleaned.split("\n")

    payload = runner.call(SEGMENT, number_lines(lines))
    draft = check_segments(payload, len(lines), config.MAX_PROBLEMS_PER_BATCH)
    if removed:
        draft.warnings.append(f"已去除 {len(removed)} 行页眉页脚")
    if not draft.ok:
        return {"error": "；".join(draft.fatal), "code": "SEGMENT_FAILED", "detail": draft.warnings}
    snapped = snap_to_headings(draft.items, lines)
    if snapped:
        draft.warnings.append(f"已把 {snapped} 道题的起点对齐到题干标题行")

    rows: List[Dict[str, Any]] = []
    for item in draft.items:
        # 题面 = 原文切片，减去"不属于题面"的行。
        # 只能整行删，且删了哪几行会写进告警——不做任何改写，原文仍是唯一来源。
        body, mapping, removed = _slice_statement(item, lines)
        # 粘贴文本与 PDF 在这里汇合：切片后立刻过题面适配（课本记号/版式修复/
        # 样例行号剥除），样例抽取、排版分块、确认页看到的都是适配后的文本。
        # 行数守恒由 adapt_statement_keep_lines 保证，模型的行号分块不会错位。
        statement = adapt_statement_keep_lines(clean_text("\n".join(body)))
        if removed:
            draft.warnings.append(f"《{item['title']}》已剔除 {removed} 行不属于题面的文字")
        # 只挡"行号切出来是空的"这一种必然错误；够不够长、像不像一道题交给模型判断
        if not statement.strip():
            draft.warnings.append(f"《{item['title']}》行号 {item['start_line']}-{item['end_line']} 切出来是空的，已跳过")
            continue
        samples = extract_samples(statement)
        if item["sample_count"] and not samples:
            draft.warnings.append(f"《{item['title']}》模型说有样例，但程序没解析出来")
        if not judge_supported(item["judge_type"]):
            draft.warnings.append(
                f"《{item['title']}》判定方式为 {item['judge_type']}，本版本不支持，生成时会被拒收"
            )
        # 分块是排版用的"拼图"：坐标要跟着删行一起重映射，否则排版会错位
        blocks = _remap_blocks(item.pop("blocks", None), mapping)
        layout: Optional[List[Dict[str, Any]]] = None
        if blocks:
            layout, inserted = normalize_layout(blocks, statement.split("\n"))
            if inserted:
                draft.warnings.append(
                    f"《{item['title']}》模型漏标 {inserted} 行，已按正文补齐（不影响文字）"
                )
        rows.append(
            {**item, "statement_md": statement, "samples": samples, "figures": [], "layout": layout}
        )

    if not rows:
        return {"error": "模型没有切出可入库的题目", "code": "SEGMENT_FAILED", "detail": draft.warnings}

    # 图归属：按页码与题目行号区间做几何匹配，不额外调模型
    if figures:
        draft.warnings.extend(attach_figures_by_page(rows, figures, page_ranges or []))

    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO import_batches(source_name, kind, raw_text, status, pdf_key, created_at)"
            " VALUES(?,?,?, 'segmented', ?, ?)",
            (str(source_name or "")[:120], kind, cleaned, str(pdf_key or "")[:32], _now()),
        )
        batch_id = int(cursor.lastrowid)
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO import_segments(batch_id, idx, title, start_line, end_line,"
                " statement_md, samples_json, constraints_json, judge_type, tolerance,"
                " multi_testcase, sample_count, confidence, note, figures_json, layout_json,"
                " status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    batch_id,
                    index,
                    row["title"],
                    row["start_line"],
                    row["end_line"],
                    row["statement_md"],
                    _json(row["samples"]),
                    _json(row["constraints"]),
                    row["judge_type"],
                    row["tolerance"],
                    1 if row["multi_testcase"] else 0,
                    len(row["samples"]),
                    row["confidence"],
                    row["note"],
                    _json(row.get("figures") or []),
                    _json(row.get("layout") or []),
                    _now(),
                ),
            )
    return {
        "ok": True,
        "batch_id": batch_id,
        "bank": banks.view(db, bank, role),
        "library_total": int(db.query_scalar(
            "SELECT COUNT(*) FROM problems WHERE bank_id=?", (int(bank["id"]),), 0
        )),
        "library_cap": banks.cap_of(bank),
        "segments": [
            {
                "idx": index,
                "title": row["title"],
                "lines": f"{row['start_line']}-{row['end_line']}",
                "chars": len(row["statement_md"]),
                "samples": len(row["samples"]),
                "figures": len(row.get("figures") or []),
                "judge_type": row["judge_type"],
                "confidence": row["confidence"],
                "note": row["note"],
            }
            for index, row in enumerate(rows, start=1)
        ],
        "warnings": draft.warnings,
    }


def _save_figures(
    db: Any, problem_id: int, figures: Sequence[Dict[str, Any]], pdf_key: str
) -> int:
    """按已算好的 bbox 从缓存 PDF 裁剪落盘，并写 `problem_figures`。

    图在切题时只拿到坐标（很小），到这一步才真正裁图：用户没勾选的题不裁，
    磁盘上不会留一堆没人引用的 PNG。
    """
    if not figures or not pdf_key:
        return 0
    cache = config.DATA_DIR / "pdf_cache" / f"{pdf_key}.pdf"
    if not cache.is_file():
        return 0
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return 0
    from ..app.pdf_figures import FigureBlock, crop_figure_png

    out_dir = config.DATA_DIR / "assets" / f"problem_{problem_id}"
    saved = 0
    try:
        with pymupdf.open(str(cache)) as doc:
            out_dir.mkdir(parents=True, exist_ok=True)
            with db.transaction() as conn:
                conn.execute("DELETE FROM problem_figures WHERE problem_id=?", (int(problem_id),))
                for ordinal, figure in enumerate(figures, start=1):
                    bbox = figure.get("bbox") or []
                    if len(bbox) != 4:
                        continue
                    block = FigureBlock(
                        id=str(figure.get("id") or f"fig_{ordinal}"),
                        page=int(figure.get("page") or 1),
                        bbox=tuple(float(value) for value in bbox),
                        kind="",
                        width=float(figure.get("width") or 0),
                        height=float(figure.get("height") or 0),
                    )
                    data = crop_figure_png(doc, block)
                    (out_dir / f"{block.id}.png").write_bytes(data)
                    conn.execute(
                        "INSERT INTO problem_figures(problem_id, label, page, bbox_json, path,"
                        " width, height, after_line, ordinal, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(problem_id),
                            block.id,
                            block.page,
                            _json([float(value) for value in bbox]),
                            f"/assets/problem_{problem_id}/{block.id}.png",
                            block.width,
                            block.height,
                            int(figure.get("after_line") or 1),
                            ordinal,
                            _now(),
                        ),
                    )
                    saved += 1
    except Exception:  # noqa: BLE001 - 裁图失败不该让整题入库失败
        return saved
    return saved


#: 定义式显示公式：行以「lhs = 大运算符」开头（f(S) = ⊕ᵢ∈S aᵢ）。
#: 这类公式竖排转出来的单行文本（⊕ᵢ∈S aᵢ）语义对但排版不是原文，
#: 学生看得别扭——从缓存 PDF 裁原图，前端在该行位置用图替换文字。
_MATH_DEF_RE = re.compile(r"^\s*([^\s，。；、]{1,15}?)\s*=\s*([⊕∑∏∫⊗])")


def _math_def_lines(statement: str) -> List[Tuple[int, str, str]]:
    """题面里的定义式公式 → [(行号, lhs, 大运算符)]。"""
    out: List[Tuple[int, str, str]] = []
    for no, line in enumerate(str(statement).split("\n"), start=1):
        match = _MATH_DEF_RE.match(line)
        if match:
            out.append((no, match.group(1).strip(), match.group(2)))
    return out


def _find_formula_bbox(doc: Any, lhs: str) -> Optional[Tuple[int, Tuple[float, float, float, float]]]:
    """在 PDF 文本层定位 `lhs =` 锚点行，把右侧相邻的小字行（大运算符与上下限）并进 bbox。

    只收锚点右侧、纵向 ±2.2 倍行高内的短行：竖排公式的 ⊕、i∈S、aᵢ 都符合；
    正文段落行（起点在左版心、或宽度超 120pt）一律排除。
    """
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return None
    key = (lhs + "=").replace(" ", "")
    for page_index in range(int(doc.page_count)):
        page = doc.load_page(page_index)
        try:
            data = page.get_text("dict")
        except Exception:  # noqa: BLE001
            continue
        lines: List[Tuple[Any, str]] = []
        for block in data.get("blocks", []):
            if int(block.get("type", -1)) != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(str(sp.get("text") or "") for sp in line.get("spans", []))
                if text.strip():
                    lines.append((pymupdf.Rect(line["bbox"]), text))
        for anchor, text in lines:
            if text.replace(" ", "") != key:
                continue
            height = float(anchor.y1) - float(anchor.y0)
            top = float(anchor.y0) - 2.2 * height
            bottom = float(anchor.y1) + 2.2 * height
            # 右侧候选按 x0 排序，只收与前一部件横向间隙 ≤3 倍行高的短行——
            # 竖排公式的 ⊕、i∈S、aᵢ 贴着锚点，同排的后续正文隔着大空隙
            candidates: List[Tuple[Any, float]] = []
            for rect, other in lines:
                if rect is anchor or float(rect.width) > 120.0:
                    continue
                if float(rect.x0) < float(anchor.x1) - 3.0:
                    continue
                if float(rect.y1) < top or float(rect.y0) > bottom:
                    continue
                candidates.append((rect, float(rect.x0)))
            candidates.sort(key=lambda item: item[1])
            members = [anchor]
            cursor_x = float(anchor.x1)
            for rect, _x0 in candidates:
                if float(rect.x0) - cursor_x > 3.0 * height:
                    break
                members.append(rect)
                cursor_x = max(cursor_x, float(rect.x1))
                if len(members) >= 10:
                    break
            merged = members[0]
            for rect in members[1:]:
                merged = merged | rect
            return page_index + 1, (
                float(merged.x0),
                float(merged.y0),
                float(merged.x1),
                float(merged.y1),
            )
    return None


def attach_math_figures(db: Any, problem_id: int, pdf_key: str, statement: str) -> int:
    """定义式公式从缓存 PDF 裁原图作插图，label 为 `math_<行号>`。

    前端见到 `math_` 前缀的图会把对应行文字替换成图（而不是追加显示），
    题面行数不变，layout 分块与既有插图定位不受影响。
    """
    if not pdf_key:
        return 0
    cache = config.DATA_DIR / "pdf_cache" / f"{pdf_key}.pdf"
    if not cache.is_file():
        return 0
    targets = _math_def_lines(statement)
    if not targets:
        return 0
    try:
        import pymupdf  # type: ignore
    except ImportError:
        return 0
    from ..app.pdf_figures import FigureBlock, crop_figure_png

    out_dir = config.DATA_DIR / "assets" / f"problem_{problem_id}"
    saved = 0
    try:
        with pymupdf.open(str(cache)) as doc:
            for line_no, lhs, _op in targets:
                found = _find_formula_bbox(doc, lhs)
                if not found:
                    continue
                page, bbox = found
                out_dir.mkdir(parents=True, exist_ok=True)
                fig_id = f"math_{line_no}"
                block = FigureBlock(
                    id=fig_id,
                    page=page,
                    bbox=bbox,
                    kind="math",
                    width=bbox[2] - bbox[0],
                    height=bbox[3] - bbox[1],
                )
                data = crop_figure_png(doc, block, dpi=220, pad=4.0)
                (out_dir / f"{fig_id}.png").write_bytes(data)
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM problem_figures WHERE problem_id=? AND label=?",
                        (int(problem_id), fig_id),
                    )
                    conn.execute(
                        "INSERT INTO problem_figures(problem_id, label, page, bbox_json, path,"
                        " width, height, after_line, ordinal, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(problem_id),
                            fig_id,
                            page,
                            _json([round(float(v), 1) for v in bbox]),
                            f"/assets/problem_{problem_id}/{fig_id}.png",
                            block.width,
                            block.height,
                            int(line_no),
                            900 + int(line_no),
                            _now(),
                        ),
                    )
                saved += 1
    except Exception:  # noqa: BLE001 - 公式截图失败不该让整题入库失败
        return saved
    return saved


def confirm_import(
    db: Any,
    batch_id: int,
    indices: Sequence[int],
    *,
    bank_id: int = banks.DEFAULT_ID,
    role: str = "student",
    user: str = "",
) -> Dict[str, Any]:
    bank = banks.get(db, bank_id)
    if bank is None:
        return {"error": "题库不存在", "code": "NOT_FOUND"}
    # 权限在入库这一刻再判一次：切题时可能是管理员，确认时可能是另一个人；
    # 而且接口是公开的，不能只靠前端把按钮藏起来。
    if not banks.can_manage(bank, role):
        return {"error": banks.DENY_ADD, "code": "FORBIDDEN"}
    batch = db.query_one("SELECT * FROM import_batches WHERE id=?", (int(batch_id),))
    if batch is None:
        return {"error": "导入批次不存在", "code": "NOT_FOUND"}
    wanted = {int(item) for item in indices} if indices else None
    rows = db.query_all(
        "SELECT * FROM import_segments WHERE batch_id=? ORDER BY idx", (int(batch_id),)
    )
    imported: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        index = int(row["idx"])
        if wanted is not None and index not in wanted:
            continue
        if str(row["status"]) != "pending":
            skipped.append({"idx": index, "reason": f"状态为 {row['status']}"})
            continue
        statement = normalize_statement(str(row["statement_md"]))
        digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()
        # 查重按库算：同一道题可以在默认题库和永久库各存一份
        existed = db.query_one(
            "SELECT id FROM problems WHERE bank_id=? AND statement_hash=?",
            (int(bank["id"]), digest),
        )
        if existed is not None:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE import_segments SET status='duplicate', problem_id=? WHERE id=?",
                    (int(existed["id"]), int(row["id"])),
                )
            duplicates.append({"idx": index, "problem_id": int(existed["id"]), "title": row["title"]})
            continue
        now = _now()
        # 难度在入库这一刻评估：题目列表"待生成"栏直接显示，学生选题前就有预期。
        # 规则评估失败留空，不拦导入。
        try:
            _constraints = json.loads(str(row["constraints_json"] or "[]"))
            if not isinstance(_constraints, list):
                _constraints = []
            difficulty = difficulty_mod.estimate(
                statement, _constraints, judge_type=str(row["judge_type"] or "")
            )
        except Exception:  # noqa: BLE001
            difficulty = ""
        # 样例行号兜底剥除：切题阶段存的样例可能还带着 CSP 题面的行号前缀
        try:
            _samples = json.loads(str(row["samples_json"] or "[]"))
            if isinstance(_samples, list):
                _samples = _strip_packed_samples(_samples)
            else:
                _samples = []
        except (json.JSONDecodeError, TypeError):
            _samples = []
        samples_json = _json(_samples)
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash, samples_json,"
                " constraints_json, judge_type, tolerance, multi_testcase, layout_json,"
                " difficulty, status, dataset_version, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'draft', 0, ?, ?, ?)",
                (
                    int(bank["id"]),
                    str(row["title"]),
                    statement,
                    digest,
                    samples_json,
                    str(row["constraints_json"]),
                    str(row["judge_type"]),
                    str(row["tolerance"]),
                    int(row["multi_testcase"]),
                    str(row["layout_json"] or "[]"),
                    difficulty,
                    str(user or ""),
                    now,
                    now,
                ),
            )
            problem_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE import_segments SET status='confirmed', problem_id=? WHERE id=?",
                (problem_id, int(row["id"])),
            )
        imported.append({"idx": index, "problem_id": problem_id, "title": row["title"]})
        # 题建好后再裁图：没勾选的题不产生文件
        try:
            figure_list = json.loads(str(row["figures_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            figure_list = []
        if isinstance(figure_list, list) and figure_list:
            count = _save_figures(db, problem_id, figure_list, str(batch["pdf_key"] or ""))
            imported[-1]["figures"] = count
            if count == 0 and batch["pdf_key"]:
                imported[-1]["figure_warning"] = "插图裁剪失败，可重新导入该 PDF"
        # 定义式公式（f(S) = ⊕ᵢ∈S aᵢ 这类）再补一张原图：文本行保留语义，
        # 显示层由前端用图替换，排版与原 PDF 一致
        try:
            math_count = attach_math_figures(
                db, problem_id, str(batch["pdf_key"] or ""), statement
            )
            if math_count:
                imported[-1]["figures"] = int(imported[-1].get("figures") or 0) + math_count
        except Exception:  # noqa: BLE001 - 公式截图是增强功能，失败不影响入库
            pass

    # 临时库每次确认入库都把数量压回上限：超出的删最早入库的，留下的就是最新一批。
    # 永久库在这一步直接返回 0：不设上限，也不自动删题。
    trimmed = 0
    try:
        trimmed = problem_purge.trim_to_cap(db, bank_id=int(bank["id"]))
    except Exception as exc:  # noqa: BLE001 - 上限清理是收尾动作，不该判入库失败
        print(f"题库上限清理失败：{exc}", flush=True)
    # 单批就超过上限时，刚入库的题也可能被清掉；别让前端去打开一道已经不在的题
    imported = [
        item
        for item in imported
        if db.query_one("SELECT 1 FROM problems WHERE id=?", (int(item["problem_id"]),))
    ]
    if imported:
        with db.transaction() as conn:
            conn.execute("UPDATE import_batches SET status='confirmed' WHERE id=?", (int(batch_id),))
    return {
        "ok": True,
        "imported": imported,
        "duplicates": duplicates,
        "skipped": skipped,
        "bank": banks.view(db, bank, role),
        "library_total": int(db.query_scalar(
            "SELECT COUNT(*) FROM problems WHERE bank_id=?", (int(bank["id"]),), 0
        )),
        "library_cap": banks.cap_of(bank),
        "library_replaced": trimmed,
    }


def batch_detail(db: Any, batch_id: int) -> Dict[str, Any]:
    batch = db.query_one("SELECT * FROM import_batches WHERE id=?", (int(batch_id),))
    if batch is None:
        raise KeyError(batch_id)
    rows = db.query_all("SELECT * FROM import_segments WHERE batch_id=? ORDER BY idx", (int(batch_id),))
    return {
        "batch_id": int(batch["id"]),
        "status": str(batch["status"]),
        "created_at": str(batch["created_at"]),
        "segments": [
            {
                "idx": int(row["idx"]),
                "title": str(row["title"]),
                "lines": f"{int(row['start_line'])}-{int(row['end_line'])}",
                "chars": len(str(row["statement_md"])),
                "samples": int(row["sample_count"]),
                "judge_type": str(row["judge_type"]),
                "confidence": str(row["confidence"]),
                "status": str(row["status"]),
                "problem_id": int(row["problem_id"]) if row["problem_id"] else None,
            }
            for row in rows
        ],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
