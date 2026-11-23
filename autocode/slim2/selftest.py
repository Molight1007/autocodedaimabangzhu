"""离线自测：用假 LLM 跑通 导入 → 确认 → D2/D3 → D4/D5。

不调用真实模型、不花钱；验证的是"接线与门禁"，不是"模型水平"。
跑法：`python -m autocode.slim2.selftest`
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List

from . import config

_TMP = config.ROOT / ".tmp"
_DB = _TMP / "slim2-selftest.db"
_CRED = _TMP / "slim2-selftest-credentials.json"

_PASS = 0
_FAIL: List[str] = []
_OPEN_DB: Any = None


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL.append(f"{name} {detail}")
        print(f"  FAIL  {name}  {detail}")


def clean() -> None:
    for path in (_DB, _CRED):
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    try:
        (_TMP / "slim2-initial-password.txt").unlink(missing_ok=True)
    except OSError:
        pass


# ── 固定题面 ────────────────────────────────────────────────────────────────
TEXT = """题目一 区间最大值
【题目描述】
给定 n 个整数，求其中的最大值。输入保证 1<=n<=10000，每个整数绝对值不超过十的九次方。
请输出一个整数，表示这 n 个数中的最大值。
【输入格式】
第一行一个整数 n，第二行 n 个整数，空格分隔。
【输出格式】
一个整数，表示最大值。
【样例输入】
3
1 2 3
【样例输出】
3

题目二 两数之和
【题目描述】
给定两个整数 a 和 b，输出它们的和。输入保证绝对值不超过十的九次方。
【输入格式】
一行两个整数 a b。
【输出格式】
一个整数，表示 a+b。
【样例输入】
1 2
【样例输出】
3
"""


def _ranges(text: str) -> Dict[str, Any]:
    lines = text.split("\n")
    first = next(i for i, line in enumerate(lines, 1) if line.startswith("题目一"))
    second = next(i for i, line in enumerate(lines, 1) if line.startswith("题目二"))
    return {
        "total": len(lines),
        "problems": [
            {"title": "区间最大值", "start_line": first, "end_line": second - 1,
             "judge_type": "token", "tolerance": "", "multi_testcase": False,
             "constraints": ["1 <= n <= 10000"], "sample_count": 1,
             "confidence": "high", "note": ""},
            {"title": "两数之和", "start_line": second, "end_line": len(lines),
             "judge_type": "token", "tolerance": "", "multi_testcase": False,
             "constraints": ["|a|,|b| <= 10^9"], "sample_count": 1,
             "confidence": "high", "note": ""},
        ],
    }


def _arr(values: List[int]) -> str:
    return f"{len(values)}\n" + " ".join(str(v) for v in values) + "\n"


def _dataset_payload() -> Dict[str, Any]:
    cases = [
        ("boundary", "tiny", _arr([-5]), "-5\n", "n=1，唯一元素 -5"),
        ("boundary", "tiny", _arr([0]), "0\n", "n=1，唯一元素 0"),
        ("boundary", "tiny", _arr([5, -2]), "5\n", "5 比 -2 大"),
        ("boundary", "tiny", _arr([-5, -2]), "-2\n", "-2 比 -5 大"),
        ("typical", "small", _arr([1, 2, 3, 4, 5]), "5\n", "递增序列最大是 5"),
        ("typical", "small", _arr([-1, -2, -3, -4]), "-1\n", "全负最大是 -1"),
        ("typical", "small", _arr([0, 0, 0, 0, 0, 0]), "0\n", "全零最大是 0"),
        ("typical", "small", _arr([10, -10, 0]), "10\n", "最大是 10"),
        ("stress", "large", _arr([7] * 300), "7\n", "300 个 7，最大 7"),
        ("stress", "large", _arr(list(range(1, 301))), "300\n", "递增到 300"),
        ("trap", "tiny", _arr([3, 9, 9, 2]), "9\n", "最大值并列，仍是 9"),
        ("trap", "tiny", _arr([-1, -1, -1]), "-1\n", "全负且相同，最大 -1"),
    ]
    return {
        # 契约：题面写的是"第一行 n，第二行 n 个整数"
        "io": {
            "input": [
                {"name": "n", "type": "int", "min": 1, "max": 10000},
                {"name": "a", "type": "int[]", "len": "n"},
            ],
            "output": {"type": "int", "order": "fixed"},
        },
        "judge_type": "token",
        "samples": [{"input": "3\n1 2 3\n", "output": "3\n"}],
        "analysis": {
            "input_model": "第一行 n，第二行 n 个整数",
            "answer_model": "一个整数：最大值",
            "constraint_dims": [{"name": "n", "min": "1", "max": "10000"}],
            "risk_points": ["全负", "n=1", "最大值并列"],
            "unsupported_reason": "",
        },
        "plan": {
            "target_count": 12,
            "layers": {"boundary": 4, "typical": 4, "stress": 2, "trap": 2},
            "coverage": ["n=1", "全负", "全零", "并列"],
        },
        "cases": [
            {"layer": layer, "size_class": size, "input": data,
             "derive": derive, "expected_output": expected, "confidence": "high"}
            for layer, size, data, expected, derive in cases
        ],
    }


STUDENT_CODE = """import sys
data = list(map(int, sys.stdin.read().split()))
n = data[0]
arr = data[1:1 + n]
best = 0
for value in arr:
    if value > best:
        best = value
print(best)
"""

#: D2R 的参考解：这份会被**真跑**，用它算出的值核对 D2 的口算。
REFERENCE_CODE = """import sys
data = sys.stdin.buffer.read().split()
numbers = [int(x) for x in data[1:]]
sys.stdout.write(str(max(numbers)) + "\\n")
"""


def main() -> int:
    _TMP.mkdir(parents=True, exist_ok=True)
    clean()
    import os

    os.environ["AUTOCODE_SLIM2_DB"] = str(_DB)
    os.environ["AUTOCODE_SLIM2_CREDENTIALS"] = str(_CRED)
    os.environ.pop("AUTOCODE_SLIM2_PASSWORD_HASH", None)
    config.apply_env()

    from . import segment
    from .adversarial import run_adversarial
    from .dataset import run_dataset
    from .db import get_db
    from .llm_stage import StageRunner
    from .validators import code_hash

    global _OPEN_DB
    db = get_db(_DB)
    _OPEN_DB = db
    ranges = _ranges(segment.clean_text(TEXT))
    d2 = _dataset_payload()
    d4_cases = [
        {"input": "3\n-5 -2 -9\n", "derive": "三个负数里最大的是 -2", "expected_output": "-2\n",
         "predicted_student_output": "0\n", "targets": "全负数",
         "failure_reason": "best 初始化为 0，全负数时 if value > best 一次都不成立，最后输出 0 而不是 -2",
         "confidence": "high"},
        {"input": "3\n1 2 3\n", "derive": "1 2 3 的最大值是 3", "expected_output": "3\n",
         "predicted_student_output": "3\n", "targets": "普通输入",
         "failure_reason": "这条本身卡不住，用来验证真跑会把它筛掉",
         "confidence": "high"},
    ]

    def responder(stage: str, user: str, _system: str) -> Dict[str, Any]:
        if stage == "slim_segment":
            return ranges
        if stage == "slim_dataset":
            return d2
        if stage == "slim_dataset_review":
            indices = [int(item) for item in re.findall(r"\[index=(\d+)\]", user)]
            return {
                "contract": "第一行 n，第二行 n 个整数；输出最大值",
                "reviews": [
                    {"index": index, "input_ok": True,
                     "recompute": f"逐项比较得 {d2['cases'][index]['expected_output'].strip()}",
                     "recomputed_output": d2["cases"][index]["expected_output"],
                     "verdict": "pass", "reason": "", "corrected_output": None}
                    for index in indices
                ],
                "coverage": {"missing": [], "verdict": "ok"},
            }
        if stage == "slim_reference":
            return {"code": REFERENCE_CODE}
        if stage == "slim_adversarial":
            return {
                "diagnosis": {"divergence": "第 6 行 best 初始化为 0，全负时返回 0",
                              "trigger_condition": "所有元素为负", "failure_mode": "WA",
                              "confidence": "high"},
                "cases": d4_cases,
            }
        if stage == "slim_adversarial_review":
            indices = sorted({int(item) for item in re.findall(r"\[index=(\d+)\]", user)})
            return {"reviews": [
                {"index": index, "input_ok": True, "recompute": "三个负数最大是 -2",
                 "recomputed_output": "-2\n", "verdict": "accept", "reason_code": "ok",
                 "reason": "", "corrected_output": None}
                for index in indices
            ]}
        raise AssertionError(f"未知阶段 {stage}")

    print("== 1. 切题与确认 ==")
    runner = StageRunner(db, budget=4, responder=responder)
    imported = segment.segment_text(db, runner, TEXT, source_name="selftest")
    check("切出 2 道题", imported.get("batch_id") and len(imported.get("segments", [])) == 2, str(imported))
    batch_id = int(imported["batch_id"])
    confirmed = segment.confirm_import(db, batch_id, [1, 2])
    check("入库 2 道", len(confirmed.get("imported", [])) == 2, str(confirmed))
    problems = [dict(row) for row in db.query_all("SELECT * FROM problems ORDER BY id")]
    check("题目落库", len(problems) == 2, str(len(problems)))
    samples = json.loads(problems[0]["samples_json"])
    check("样例解析", len(samples) == 1 and samples[0]["output"].strip() == "3", str(samples))
    again = segment.confirm_import(db, batch_id, [1, 2])
    check("重复确认被跳过", not again.get("imported") and len(again.get("skipped", [])) >= 1, str(again))

    print("== 2. D2/D3 数据流水线 ==")
    pid = int(problems[0]["id"])
    runner2 = StageRunner(db, problem_id=pid, budget=6, responder=responder)
    result = run_dataset(db, pid, runner2)
    check("入库条数 ≥10", int(result["accepted"]) >= 10, str(result))
    rows = [dict(row) for row in db.query_all(
        "SELECT * FROM test_cases WHERE problem_id=? AND version=1 AND status='active'", (pid,)
    )]
    check("含题面样例", any(row["source"] == "sample" for row in rows), str(len(rows)))
    check("全部标记已复核", all(row["trust"] == "cross_verified" for row in rows if row["source"] == "generated"))
    check("版本号 +1", int(db.query_one("SELECT dataset_version FROM problems WHERE id=?", (pid,))[0]) == 1)
    check("llm_calls 有记录", db.query_scalar("SELECT COUNT(*) FROM llm_calls WHERE problem_id=?", (pid,), 0) >= 2)
    quality = json.loads(str(db.query_one("SELECT quality_json FROM problems WHERE id=?", (pid,))[0]))
    run_check = quality.get("run_verification") or {}
    check("代码答案已执行", bool(run_check.get("engaged")), str(run_check))
    check("样例全部通过", str(run_check.get("samples")) == "1/1", str(run_check))
    check("样例锚定生效", bool(run_check.get("anchored")), str(run_check))
    check("执行覆盖面达标",
          int((run_check.get("verdicts") or {}).get("executed", 0)) >= 12, str(run_check))

    print("== 3. D4/D5 对抗流水线 ==")
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO submissions(problem_id, lang, code, code_hash, status, created_at)"
            " VALUES(?,?,?,?, 'new', ?)",
            (pid, "python", STUDENT_CODE, code_hash(STUDENT_CODE), "2026-11-20T00:00:00+00:00"),
        )
        submission_id = int(cursor.lastrowid)
    runner3 = StageRunner(db, problem_id=pid, budget=4, responder=responder)
    adv = run_adversarial(db, pid, submission_id, runner3)
    check("卡住 1 条", int(adv.get("caught", 0)) == 1, str(adv))
    adv_rows = [dict(row) for row in db.query_all(
        "SELECT * FROM adversarial_cases WHERE problem_id=? AND status='active'", (pid,)
    )]
    check("对抗数据入库", len(adv_rows) == 1, str(len(adv_rows)))
    check("实际输出为 0", bool(adv_rows) and adv_rows[0]["actual_output"].strip() == "0",
          adv_rows[0]["actual_output"] if adv_rows else "none")
    check("运行判定 WA", bool(adv_rows) and adv_rows[0]["run_verdict"] == "WA")
    check("错误原因已落库", bool(adv_rows) and "初始化" in str(adv_rows[0]["failure_reason"]),
          adv_rows[0]["failure_reason"] if adv_rows else "none")

    _check_validators(d2)

    print("")
    print(f"通过 {_PASS} 项，失败 {len(_FAIL)} 项")
    for item in _FAIL:
        print("  - " + item)
    return 0 if not _FAIL else 1


def _check_validators(d2: Dict[str, Any]) -> None:
    """校验器边界：判定方式、浮点容差、同批重复、空推导。"""
    from .validators import check_dataset, check_dataset_review, compare_output

    print("== 4. 校验器边界 ==")
    ok, _ = compare_output("token", "3\n", "3 \n")
    check("token 忽略行尾空白", ok)
    bad, _ = compare_output("token", "3\n", "4\n")
    check("token 能发现不同", not bad)
    fok, _ = compare_output("float", "1.0000000\n", "1.0000001\n", "1e-6")
    check("浮点容差生效", fok)
    dup = check_dataset(
        {"analysis": {"unsupported_reason": ""},
         "plan": {"target_count": 10,
                  "layers": {"boundary": 3, "typical": 3, "stress": 2, "trap": 2},
                  "coverage": []},
         "cases": [d2["cases"][0]] * 10},
        min_cases=10, max_cases=40, samples=[], judge_type="token", tolerance="",
    )
    check("同批重复被拒", not dup.ok, str(dup.fatal))
    rev = check_dataset_review(
        {"reviews": [{"index": 0, "input_ok": True, "recompute": "与期望一致",
                      "recomputed_output": "3\n", "verdict": "pass"}]},
        [{"index": 0, "expected_output": "3\n"}], "token", "",
    )
    check("空推导不算通过", rev.ok and rev.items[0]["verdict"] == "uncertain", str(rev.items))


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        try:
            if _OPEN_DB is not None:
                _OPEN_DB.close()
        except Exception:  # noqa: BLE001
            pass
        clean()
    sys.exit(code)
