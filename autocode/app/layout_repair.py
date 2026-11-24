"""通用版式修复：把 PDF 抽取带来的"伪数据"还原成真实输入。

设计口径（面向所有题，而非单题）：
- 只做**可验证的删除**：删掉的 token 必须恰好是 1..k 的连续行号，否则不删；
- 不猜语义：不认识的结构原样返回，交由上层判定；
- 每一处改动都记 note，便于回溯。

覆盖四类通用损坏：
1. 不可见/异体字符：U+2010 连字符、NBSP、零宽字符、全角数字；
2. 页眉页脚：`第 k 页 共 n 页`、`CCF CSP ...`、`第 k 次认证 ...`；
3. 行号列：独立成行的 1..k、前缀 `k 内容`、以及 PDF 压平后的合并行；
4. 合并行：`1 n m 2 a1..an 3 b1..bm` 这类"行号与数据压成一行"。
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

_PAGE_HEAD = re.compile(r"^第\s*\d+\s*页")
_PAGE_FOOT = re.compile(r"^共\s*\d+\s*页")
_CCF = re.compile(r"^CCF\s*CSP")
_ROUND = re.compile(r"^第\s*\d+\s*次认证")
_PREFIX_NO = re.compile(r"^(\d+)\s+(.*)$")


def normalize_chars(text: str) -> str:
    table = {
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
        "\u2212": "-", "\u00a0": " ", "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
    }
    for src, dst in table.items():
        text = text.replace(src, dst)
    # 全角数字 → 半角
    return text.translate({0xFF10 + i: ord("0") + i for i in range(10)})


def drop_page_artifacts(lines: Sequence[str]) -> List[str]:
    kept = []
    for line in lines:
        item = line.strip()
        if not item:
            continue
        if _PAGE_HEAD.match(item) or _PAGE_FOOT.match(item) or _CCF.match(item) or _ROUND.match(item):
            continue
        # 抽取时插入的分页注释（<!-- Page 5 -->）也不属于样例数据
        if item.startswith("<!--") and item.endswith("-->"):
            continue
        kept.append(item)
    return kept


def strip_number_column(lines: Sequence[str]) -> Tuple[List[str], bool]:
    """剥离行号列：支持"独立成行交替"与"k 内容"两种形态。"""
    items = list(lines)
    # 形态 A：奇数位是 1..k，偶数位是内容
    if len(items) >= 4 and len(items) % 2 == 0:
        odds = items[0::2]
        if all(x.isdigit() for x in odds) and [int(x) for x in odds] == list(range(1, len(odds) + 1)):
            return items[1::2], True
    # 形态 B：每行 "k 内容"
    matches = [_PREFIX_NO.match(x) for x in items]
    if len(items) >= 3 and all(matches) and [int(m.group(1)) for m in matches] == list(range(1, len(items) + 1)):
        return [m.group(2).strip() for m in matches if m], True
    return items, False


_LINE_NO_RE = re.compile(r"^\s*(\d+)[ \t]+(\S.*)$")


def strip_line_numbers(
    lines: Sequence[str], *, force_single: bool = False
) -> Tuple[List[str], bool]:
    """剥离样例框左侧的行号列。

    判定条件（同时满足才剥）：
    - 每行都是 `<序号><空白><正文>`；
    - 序号恰好是 1..k 连续；
    - 至少 2 行；**单行块**只有在 `force_single=True`（文档级确认所有样例框都带行号）时才剥，
      否则无法区分"真数据首列恰好是 1"与"行号 1"。
    """
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        return list(lines), False
    if len(nonempty) == 1 and not force_single:
        return list(lines), False
    matches = [_LINE_NO_RE.match(line) for line in nonempty]
    if any(m is None for m in matches):
        return list(lines), False
    for expect, match in enumerate(matches, start=1):
        assert match is not None
        if int(match.group(1)) != expect:
            return list(lines), False
    return [match.group(2).strip() for match in matches if match], True


def restore_merged_lines(text: str) -> Tuple[str, bool]:
    """还原"行号与数据压成一行"的三种常见形态（只删 1/2/3 号行号，删后必须更短）。

    只在**确实被压平**时启用：文本已是一行一个 token 的正常换行结构时，
    强行按 token 重排会把 `1\\n5 2\\n2\\n10 50...` 误判成 "n m + 两段数组"，
    把后面的数据整段截掉（CSP32 树上搜索的真实事故）。
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    # 真正的“压平”只会出现在**同一行**里；两行以上说明换行结构还在。
    # 且必须以行号 1 开头（样例框左侧带行号列），否则 `5 2\n10 50 10 10 20`
    # 会被误判成 “n=5 + 行号2 + 5 个数”，把 m=2 当行号删掉（实测会改坏数据）。
    if len(lines) != 1:
        return text, False
    tokens = text.split()
    if not tokens or tokens[0] != "1":
        return text, False
    if not tokens:
        return text, False
    # 首行行号 1 与 n、m 压在一起时，先去掉这个 1
    if len(tokens) >= 4 and tokens[0] == "1" and tokens[1].isdigit() and tokens[2].isdigit():
        tokens = tokens[1:]
    # 形态 1：单数组 n + 行号2 + n 个数
    if len(tokens) >= 3 and tokens[0].isdigit():
        n = int(tokens[0])
        if tokens[1] == "2" and len(tokens) == n + 2:
            return f"{n}\n" + " ".join(tokens[2:]) + "\n", True
    # 形态 2：两段文本 n m + 行号2 + n 个词 + 行号3 + m 个词
    if len(tokens) >= 4 and tokens[0].isdigit() and tokens[1].isdigit():
        n, m = int(tokens[0]), int(tokens[1])
        rest = tokens[2:]
        if len(rest) >= n + m + 2 and rest[0] == "2" and rest[1 + n] == "3":
            first, second = rest[1 : 1 + n], rest[2 + n : 2 + n + m]
            if len(first) == n and len(second) == m:
                return f"{n} {m}\n" + " ".join(first) + "\n" + " ".join(second) + "\n", True
    return text, False


#: 行首出现这些标记，本行及其后都不是数据（"解释：342 + 465 = 807." 只有 2 个汉字，
#: 靠字数阈值抓不到；LeetCode 风格题面几乎每条输出后面都跟一行）。
PROSE_MARKERS = (
    "解释：", "解释:", "说明：", "说明:", "提示：", "提示:",
    "示例说明", "Explanation:", "Note:",
)


def sanitize_sample(input_text: str, output_text: str) -> Tuple[str, str, List[str]]:
    """清洗一组样例，返回 (input, output, notes)。"""
    notes: List[str] = []
    inp = normalize_chars(str(input_text or ""))
    out = normalize_chars(str(output_text or ""))

    # 合并行形态先处理（此时还没有可靠换行）
    restored_in, hit = restore_merged_lines(inp.strip())
    if hit:
        inp, notes = restored_in, notes + ["merged_lines_restored"]

    in_lines = drop_page_artifacts(inp.splitlines())
    out_lines = drop_page_artifacts(out.splitlines())
    in_lines, stripped_in = strip_number_column(in_lines)
    out_lines, stripped_out = strip_number_column(out_lines)
    # 输出块通常很短（1~2 行），单看它无法区分"行号 1"和"数据 1"；
    # 用输入块已确认带行号作为联动证据，再剥输出的首行行号。
    # 输出只有 1 行内容时，行号 1 无法靠交替模式识别；按"首行 1 + 恰两行"处理
    if not stripped_out and len(out_lines) == 2 and out_lines[0] == "1":
        out_lines = out_lines[1:]
        stripped_out = True
    if (stripped_in or hit) and out_lines and out_lines[0] == "1" and len(out_lines) >= 2:
        out_lines = out_lines[1:]
        stripped_out = True
    if stripped_in or stripped_out:
        notes.append("line_number_column_stripped")

    return "\n".join(in_lines) + "\n", "\n".join(out_lines) + "\n", notes
