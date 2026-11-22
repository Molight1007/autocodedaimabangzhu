"""预生成阶段的落库层：每题每阶段的写入语句集中在此（R6：全部参数化 SQL）。

为什么单独一层：
- 编排逻辑（prep_service）不该被 SQL 细节撑爆；
- 所有 upsert 都用 `ON CONFLICT DO UPDATE`（设计 R6 禁 `INSERT OR REPLACE`），
  集中一处便于审查"重跑不增行、版本号单调递增"这两件事。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from ..store.db import Database
from .prep_models import STATUS_FAILED, RefArtifact, utc_now

PROBLEM_UPDATABLE = {
    "spec_json",
    "tier",
    "status",
    "failed_stage",
    "lang_available",
    "tags_json",
    "analysis_json",
    "data_plan_json",
    "algorithm_tags",
    "artifact_version",
    "updated_at",
}

#: 单题原子性：这些表属于"重跑会覆盖"的产物，失败时要能原样还原。
_SNAPSHOT_TABLES = ("test_cases", "reference_solutions", "hints")
#: problems 里由 prepare 写入的**产物**字段。
#: 不含 status/failed_stage：失败后要保留"这次升级失败"的痕迹与原因，不能抹掉。
_PROBLEM_PRODUCT_COLS = (
    "spec_json",
    "tier",
    "lang_available",
    "analysis_json",
    "data_plan_json",
    "algorithm_tags",
    "artifact_version",
)


class PrepStore:
    """预生成产物的读写门面（持有同一份 Database）。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._table_cache: Dict[str, bool] = {}

    def table_exists(self, name: str) -> bool:
        """精简版 schema 不含 hints；快照/还原必须跳过不存在的表，而不是报错。"""
        if name not in self._table_cache:
            row = self.db.query_one(
                "SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
            self._table_cache[name] = row is not None
        return self._table_cache[name]

    def _snapshot_tables(self) -> List[str]:
        return [table for table in _SNAPSHOT_TABLES if self.table_exists(table)]

    # ── 读 ──────────────────────────────────────────────────────────────────
    def load_problem(self, problem_id: int) -> Dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM problems WHERE id = ?", (problem_id,))
        return dict(row) if row is not None else None

    def load_samples(self, problem_id: int) -> List[Dict[str, str]]:
        """优先取 spec_json.samples（ingest 落的原文），退化到 test_cases 的 sample 层。"""
        row = self.db.query_one("SELECT spec_json FROM problems WHERE id = ?", (problem_id,))
        samples: List[Dict[str, str]] = []
        if row is not None and row["spec_json"]:
            try:
                payload = json.loads(row["spec_json"])
            except json.JSONDecodeError:
                payload = {}
            for item in payload.get("samples") or []:
                if str(item.get("input") or "").strip():
                    samples.append(
                        {
                            "input": str(item.get("input") or ""),
                            "output": str(item.get("output") or ""),
                        }
                    )
        if samples:
            return samples
        for case_row in self.db.query_all(
            "SELECT input_data, expected_output FROM test_cases"
            " WHERE problem_id = ? AND layer = 'sample' ORDER BY ordinal",
            (problem_id,),
        ):
            samples.append(
                {
                    "input": str(case_row["input_data"] or ""),
                    "output": str(case_row["expected_output"] or ""),
                }
            )
        return samples

    # ── 写 ──────────────────────────────────────────────────────────────────
    def update_problem(self, problem_id: int, **fields: Any) -> None:
        payload = {key: value for key, value in fields.items() if key in PROBLEM_UPDATABLE}
        if not payload:
            return
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE problems SET {assignments} WHERE id = ?",
                list(payload.values()) + [problem_id],
            )

    def save_spec(self, problem_id: int, spec: Dict[str, Any]) -> None:
        self.update_problem(
            problem_id,
            spec_json=json.dumps(spec, ensure_ascii=False),
            tier=spec.get("tier") or "A",
        )

    def save_generator(self, problem_id: int, code: str, *, verified: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO generators(problem_id, lang, code, verified, artifact_version, created_at)"
                " VALUES(?, 'python', ?, ?, 1, ?)"
                " ON CONFLICT(problem_id, lang) DO UPDATE SET"
                "  code = excluded.code, verified = excluded.verified,"
                "  artifact_version = generators.artifact_version + 1, created_at = excluded.created_at",
                (problem_id, code, verified, utc_now()),
            )

    def save_references(self, problem_id: int, refs: Dict[str, RefArtifact]) -> None:
        with self.db.transaction() as conn:
            # 本次没产出的 (lang, role) 必须删除，否则旧代码（可能已违反注入门）会残留
            keep = {(a.lang, a.role) for a in refs.values()}
            for row in conn.execute(
                "SELECT lang, role FROM reference_solutions WHERE problem_id=?", (problem_id,)
            ).fetchall():
                if (row[0], row[1]) not in keep:
                    conn.execute(
                        "DELETE FROM reference_solutions WHERE problem_id=? AND lang=? AND role=?",
                        (problem_id, row[0], row[1]),
                    )
            for artifact in refs.values():
                conn.execute(
                    "INSERT INTO reference_solutions(problem_id, lang, role, code, verified,"
                    " verify_record, artifact_version, approach, complexity, is_primary,"
                    " samples_expected, verification_level, degrade_reason, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(problem_id, lang, role) DO UPDATE SET"
                    "  code = excluded.code, verified = excluded.verified,"
                    "  verify_record = excluded.verify_record,"
                    "  artifact_version = reference_solutions.artifact_version + 1,"
                    "  approach = excluded.approach, complexity = excluded.complexity,"
                    "  is_primary = excluded.is_primary,"
                    "  samples_expected = excluded.samples_expected,"
                    "  verification_level = excluded.verification_level,"
                    "  degrade_reason = excluded.degrade_reason, created_at = excluded.created_at,"
                    # 代码变了，按旧行号写的逐行讲解必然错位：清空让 line_notes 阶段重生成。
                    # 代码没变则原样保留，避免每次重跑都白花一次 LLM 调用。
                    "  line_notes_json = CASE WHEN reference_solutions.code <> excluded.code"
                    " THEN NULL ELSE reference_solutions.line_notes_json END",
                    (
                        problem_id,
                        artifact.lang,
                        artifact.role,
                        artifact.code,
                        int(artifact.verified),
                        json.dumps(artifact.verify_record, ensure_ascii=False),
                        artifact.approach,
                        artifact.complexity,
                        int(artifact.is_primary),
                        int(artifact.samples_total),
                        artifact.verification_level,
                        artifact.degrade_reason,
                        utc_now(),
                    ),
                )

    def save_analysis(self, problem_id: int, analysis: Dict[str, Any], tags: List[str]) -> None:
        self.update_problem(
            problem_id,
            analysis_json=json.dumps(analysis, ensure_ascii=False),
            algorithm_tags=json.dumps(tags, ensure_ascii=False),
        )

    def save_similar_local(self, problem_id: int, matches: List[Dict[str, Any]]) -> int:
        saved = 0
        with self.db.transaction() as conn:
            for item in matches:
                cur = conn.execute(
                    "INSERT INTO similar_problems(problem_id, url, title, platform, note, link_verified)"
                    " VALUES(?, ?, ?, 'local', ?, 1) ON CONFLICT(problem_id, url) DO NOTHING",
                    (problem_id, f"local:{item['id']}", item.get("title", ""), item.get("tags", "")),
                )
                saved += 1 if cur.rowcount else 0
        return saved

    def save_cases(self, problem_id: int, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """写用例；返回**去重后**的条数（按 input_hash，同一输入只保留一条）。"""
        counts: Dict[str, int] = {}
        digests: Dict[str, set[str]] = {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["layer"]), []).append(row)
        with self.db.transaction() as conn:
            for layer, items in grouped.items():
                # 重跑幂等：同一题同一层整层替换，避免随机种子不同导致用例越跑越多
                # （旧行为只 upsert，input_hash 不同就新增，两次重跑即翻倍）
                conn.execute(
                    "DELETE FROM test_cases WHERE problem_id = ? AND layer = ?",
                    (problem_id, layer),
                )
                for ordinal, item in enumerate(items, start=1):
                    digest = hashlib.sha256(str(item["input"]).encode("utf-8")).hexdigest()[:32]
                    digests.setdefault(layer, set()).add(digest)
                    conn.execute(
                        "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                        " expected_output, source_of_truth, trust_level, seed, size_class, verified_at)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(problem_id, layer, input_hash) DO UPDATE SET"
                        "  expected_output = excluded.expected_output, ordinal = excluded.ordinal,"
                        "  source_of_truth = excluded.source_of_truth,"
                        "  trust_level = excluded.trust_level, seed = excluded.seed,"
                        "  size_class = excluded.size_class, verified_at = excluded.verified_at",
                        (
                            problem_id,
                            layer,
                            ordinal,
                            str(item["input"]),
                            digest,
                            item.get("expected"),
                            item.get("source"),
                            item.get("trust"),
                            item.get("seed"),
                            item.get("size_class"),
                            utc_now(),
                        ),
                    )
                counts[layer] = len(digests.get(layer, set()))
        return counts

    def clear_generated_layers(self, problem_id: int) -> None:
        """重跑前清掉「本阶段会重新生成」的四层，避免新计划没覆盖某层时留下旧数据。

        `adversarial`（针对学生错解）与 `user`/`custom`（人工合并）不在清理范围，
        它们不由 prepare 产出，清了就是误删。
        """
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM test_cases WHERE problem_id=? AND layer IN"
                " ('sample','boundary','exhaustive','stress')",
                (problem_id,),
            )

    # ── 单题原子性：失败回滚 ────────────────────────────────────────────────
    def snapshot_problem(self, problem_id: int) -> Dict[str, Any]:
        """给单题拍一份「重跑前」快照，供失败回滚。

        为什么需要：prepare 是逐阶段边算边落库的（先换 test_cases，再写 hints/题解），
        中途失败会留下「新数据 + 旧解析」的错配，比没跑更糟——实测 #3/#9/#10 就是这样。
        失败时把旧产物原样写回，保证库永远停在「某个版本的完整产物」上。

        边界：快照只在内存里，进程被 kill 时救不了；那种情况由 `status='preparing'`
        + 下次重跑覆盖兜住（`select_targets` 把 preparing 算作目标）。
        """
        snap: Dict[str, Any] = {"problem": {}, "tables": {}}
        row = self.db.query_one("SELECT * FROM problems WHERE id = ?", (problem_id,))
        if row is not None:
            keys = row.keys()
            snap["problem"] = {col: row[col] for col in _PROBLEM_PRODUCT_COLS if col in keys}
        for table in self._snapshot_tables():
            rows = self.db.query_all(f"SELECT * FROM {table} WHERE problem_id = ?", (problem_id,))
            snap["tables"][table] = [dict(r) for r in rows]
        return snap

    def restore_problem(self, problem_id: int, snap: Dict[str, Any]) -> bool:
        """把快照写回（失败时调用）。返回是否真的还原过。

        表名/列名全部来自本模块固定白名单，值一律参数化，不拼接任何外部输入。
        """
        if not snap or not snap.get("tables"):
            return False
        with self.db.transaction() as conn:
            for table in self._snapshot_tables():
                conn.execute(f"DELETE FROM {table} WHERE problem_id = ?", (problem_id,))
                for row in snap["tables"].get(table) or []:
                    cols = list(row.keys())
                    conn.execute(
                        f"INSERT INTO {table} ({', '.join(cols)})"
                        f" VALUES ({', '.join('?' for _ in cols)})",
                        [row[c] for c in cols],
                    )
            product = snap.get("problem") or {}
            if product:
                assignments = ", ".join(f"{col} = ?" for col in product)
                conn.execute(
                    f"UPDATE problems SET {assignments} WHERE id = ?",
                    [*product.values(), problem_id],
                )
        return True

    def save_data_plan(self, problem_id: int, plan: Dict[str, Any]) -> None:
        """落库数据计划与实际产出：UI 要能展示「计划几组/实际几条」，不能只活在日志里。"""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE problems SET data_plan_json=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), problem_id),
            )

    def has_line_notes(self, problem_id: int, lang: str, role: str) -> bool:
        """只对 v3 逐行对照结构判「已有」；旧版纯文本讲解视为过期，重跑会替换。

        用字典判断而不 import app.line_notes，避免 store 层反向依赖 app 层。
        """
        row = self.db.query_one(
            "SELECT line_notes_json FROM reference_solutions"
            " WHERE problem_id=? AND lang=? AND role=?",
            (problem_id, lang, role),
        )
        raw = row["line_notes_json"] if row else None
        if not raw:
            return False
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return False
        return isinstance(data, dict) and data.get("v") == 3 and bool(data.get("lines"))

    def save_line_notes(self, problem_id: int, lang: str, role: str, payload: Any) -> None:
        """payload 是结构化 dict（v3）；只序列化一次，避免库里存成 JSON 字符串。"""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE reference_solutions SET line_notes_json=? "
                "WHERE problem_id=? AND lang=? AND role=?",
                (json.dumps(payload, ensure_ascii=False), problem_id, lang, role),
            )

    def save_hints(self, problem_id: int, rows: List[Dict[str, Any]], batch_id: str) -> None:
        """写入一批提示：旧批次先置 inactive，保证每道题只展示最新一套。

        同一批里可能有多个解法（approach），而唯一键是 (problem_id, level, gen_batch_id)，
        所以每个解法用独立 batch_id（`批次:解法`），否则同级的第二个解法会撞键。
        """
        with self.db.transaction() as conn:
            conn.execute("UPDATE hints SET is_active=0 WHERE problem_id=?", (problem_id,))
            for row in rows:
                approach = str(row.get("approach") or "")
                scoped_batch = f"{batch_id}:{approach}" if approach else batch_id
                conn.execute(
                    "INSERT INTO hints(problem_id, level, content, quality_flag, flagged_reason,"
                    " gen_batch_id, approach, is_active, regenerated_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)"
                    " ON CONFLICT(problem_id, level, gen_batch_id) DO UPDATE SET"
                    "  content = excluded.content, quality_flag = excluded.quality_flag,"
                    "  flagged_reason = excluded.flagged_reason, approach = excluded.approach,"
                    "  is_active = 1, regenerated_at = excluded.regenerated_at",
                    (
                        problem_id,
                        int(row["level"]),
                        str(row["content"]),
                        int(row.get("quality_flag") or 0),
                        str(row.get("flagged_reason") or ""),
                        scoped_batch,
                        approach,
                        utc_now(),
                    ),
                )

    def mark_failed(self, problem_id: int, stage: str, error: str) -> None:
        """题目失败落库（状态 + 阶段错误），供编排层统一调用。"""
        if not problem_id:
            return
        self.update_problem(problem_id, status=STATUS_FAILED, failed_stage=stage)
        self.record_stage_run(problem_id, stage, detail="", error=error[:4000])

    def record_llm_call(self, info: Dict[str, Any]) -> None:
        """每次 HTTP 尝试落库（含失败/重试）：token 审计的唯一真源。"""
        self.db.execute(
            "INSERT INTO llm_calls(problem_id, stage, attempt, ok, error_kind, "
            "prompt_tokens, completion_tokens, reasoning_tokens, cache_hit_tokens, "
            "total_tokens, elapsed_ms, tag, prompt_hash, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                info.get("problem_id"),
                str(info.get("stage") or ""),
                int(info.get("attempt") or 0),
                int(info.get("ok") or 0),
                str(info.get("error_kind") or ""),
                int(info.get("prompt_tokens") or 0),
                int(info.get("completion_tokens") or 0),
                int(info.get("reasoning_tokens") or 0),
                int(info.get("cache_hit_tokens") or 0),
                int(info.get("total_tokens") or 0),
                float(info.get("elapsed_ms") or 0.0),
                str(info.get("tag") or ""),
                str(info.get("prompt_hash") or ""),
                utc_now(),
            ),
        )

    def record_stage_run(
        self, problem_id: int, stage: str, *, detail: str = "", error: str = ""
    ) -> None:
        if not problem_id:
            return
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO problem_stage_runs(problem_id, stage, attempt, started_at, finished_at,"
                " error_text, output_ref) VALUES(?, ?, 1, ?, ?, ?, ?)",
                (problem_id, stage, now, now, error or "", detail[:2000]),
            )

    # ── tags（注入打标 / 批跑标记）────────────────────────────────────────────
    def load_tags(self, problem_id: int) -> Dict[str, Any]:
        row = self.db.query_one("SELECT tags_json FROM problems WHERE id = ?", (problem_id,))
        if row is None or not row["tags_json"]:
            return {}
        try:
            payload = json.loads(row["tags_json"])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def check_injection_followed(
        self, problem_id: int, codes: Dict[str, str]
    ) -> List[str]:
        """题面里被注入要求的标识符若出现在生成代码中 → 打标 `injection_followed`。

        不阻断（代码可能仍然正确），但必须在汇总里单列，交给人工判断。
        """
        tokens = [str(item) for item in (self.load_tags(problem_id).get("injection_tokens") or [])]
        if not tokens:
            return []
        hits: List[str] = []
        for where, code in codes.items():
            for name in tokens:
                if name and name in str(code or ""):
                    hit = f"{where}:{name}"
                    if hit not in hits:
                        hits.append(hit)
        if hits:
            self.merge_tags(
                problem_id, {"injection_followed": True, "injection_hits_in_code": sorted(hits)}
            )
        return hits

    def merge_tags(self, problem_id: int, extra: Dict[str, Any]) -> Dict[str, Any]:
        """合并 tags（不覆盖已有键）；返回合并后的完整 tags。"""
        tags = self.load_tags(problem_id)
        tags.update(extra)
        self.update_problem(problem_id, tags_json=json.dumps(tags, ensure_ascii=False))
        return tags
