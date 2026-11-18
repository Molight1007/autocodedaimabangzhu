"""提示阶段（⑥）：按解法分组生成 L1~L3，并给每种写法附上已验证代码作 L4。

从 `prep_service.py` 抽出：该文件受 800 行门禁约束，而提示逻辑独立、只依赖
`service._chat / service.store / service._note` 三个协作点。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

from ..prompts import loader as prompts
from . import judge
from .prep_models import MAX_HINTS_ATTEMPTS, STAGE_HINTS, StageFailure
from .prep_rules import spec_block, statement_excerpt, user_blocks


def run_hints_stage(
    service: Any,
    problem: Dict[str, Any],
    spec: Dict[str, Any],
    refs: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    base_user = (
        user_blocks(statement_excerpt(problem), spec.get("samples") or [])
        + "\n\n## 已确认的 spec\n"
        + spec_block(spec)
    )
    # 按解法分组：每种已验证写法一组 L1~L3（有代码的再附 L4）。
    # 顺序：主解 → 备选解 → 暴力解（暴力只作兜底，排在最后，UI 默认不选它）。
    role_rank = {"reference": 0, "alt1": 1, "alt2": 2, "alt3": 3, "brute": 9}
    ordered = sorted(
        refs.values(),
        key=lambda a: (role_rank.get(str(getattr(a, "role", "")), 5),
                       str(getattr(a, "approach", ""))),
    )
    methods: List[Dict[str, str]] = []
    code_by_approach: Dict[str, str] = {}
    seen_methods = set()
    for artifact in ordered:
        name = str(getattr(artifact, "approach", "") or "未标注方法")
        if name not in seen_methods:
            seen_methods.add(name)
            methods.append({
                "name": name,
                "complexity": str(getattr(artifact, "complexity", "") or "未声明"),
            })
        if getattr(artifact, "verified", 0) and name not in code_by_approach:
            code_by_approach[name] = str(getattr(artifact, "code", "") or "")
    if methods:
        base_user += (
            "\n\n## 已有解法（必须以此为准，逐条各出一组提示）\n"
            + json.dumps(methods, ensure_ascii=False)
            + "\n参考解已通过全部题面样例。"
        )
    expected = [item["name"] for item in methods] or [""]
    feedback = ""
    last_error = ""
    batch_id = f"{problem['id']}-{uuid.uuid4().hex[:8]}"
    for attempt in range(1, MAX_HINTS_ATTEMPTS + 1):
        result = service._chat(
            prompts.HINTS, base_user + feedback, tag=f"hints#{attempt}", max_tokens=8192
        )
        payload = result.json_obj or {}
        root = payload.get("root")
        root_text = str((root or {}).get("L1") or "").strip() if isinstance(root, dict) else ""
        items = payload.get("approaches")
        got: Dict[str, Dict[str, Any]] = {}
        if isinstance(items, list) and items:
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if name:
                    got[name] = item
            if not root_text:
                # 兼容 v3（每组自带 L1）：拿第一组的 L1 当根
                first = next((item for item in items if isinstance(item, dict)), {})
                root_text = str(first.get("L1") or "").strip()
        elif any(payload.get(level) for level in ("L1", "L2", "L3")):
            # 兼容 v2（平铺单组）：L1 当根，L2/L3 归到第一个解法
            got[expected[0]] = payload
            root_text = str(payload.get("L1") or "").strip()
        names = [name for name in expected if name in got]
        if not expected[0]:
            names = list(got)
        missing: Dict[str, List[str]] = {}
        if not root_text:
            missing["root"] = ["L1"]
        for name in (expected if expected[0] else names):
            source = got.get(name) or {}
            lack = [level for level in ("L2", "L3")
                    if not str(source.get(level) or "").strip()]
            if lack:
                missing[name or "（未命名）"] = lack
        if missing or not names:
            detail = "；".join(f"{name} 缺 {'/'.join(levels)}" for name, levels in missing.items())
            last_error = detail or "模型没有返回任何 approaches"
            feedback = (
                f"\n\n上一次输出不合格：{last_error}\n"
                "请按「已有解法」清单重出完整 JSON：root.L1 是面向所有解法的通用提示（不得点名算法），"
                "approaches 的条数与 name 必须与清单完全一致，每组 L2/L3 都必须非空。"
            )
            continue
        flagged: Dict[str, str] = {}
        rows: List[Dict[str, Any]] = []
        root_reason = judge.hint_quality_issue(root_text)
        if root_reason:
            flagged["root:L1"] = root_reason
        rows.append({
            "level": 0,
            "content": root_text,
            "approach": "",
            "quality_flag": 1 if root_reason else 0,
            "flagged_reason": root_reason or "",
        })
        for name in names:
            source = got.get(name) or {}
            for level, field in ((2, "L2"), (3, "L3")):
                content = str(source.get(field) or "").strip()
                reason = judge.hint_quality_issue(content)
                if reason:
                    flagged[f"{name}:{field}"] = reason
                rows.append({
                    "level": level,
                    "content": content,
                    "approach": name,
                    "quality_flag": 1 if reason else 0,
                    "flagged_reason": reason or "",
                })
            code = code_by_approach.get(name) or ""
            if code:
                rows.append({
                    "level": 4,
                    "content": code,
                    "approach": name,
                    "quality_flag": 0,
                    "flagged_reason": "",
                })
            else:
                service._note(f"{name}: L4 未生成（没有已验证代码）")
        if flagged:
            service._note(f"L1~L3 含代码特征，已打 quality_flag：{flagged}")
        service.store.save_hints(int(problem["id"]), rows, batch_id)
        detail = " ".join(
            f"{row.get('approach') or '基础'}-L{row['level']}={len(row['content'])}字"
            + ("(flag)" if row["quality_flag"] else "")
            for row in rows
        )
        summary = [
            {
                "level": row["level"],
                "approach": row.get("approach") or "",
                "chars": len(row["content"]),
                "quality_flag": row["quality_flag"],
                "flagged_reason": row["flagged_reason"],
            }
            for row in rows
        ]
        return summary, detail
    raise StageFailure(
        STAGE_HINTS, f"hints 连续 {MAX_HINTS_ATTEMPTS} 次不合格", detail=last_error
    )
