-- autoCode 数据模型（架构设计 §7，P0 冻结口径）
-- 执行约定：
--   * 连接工厂 autocode/store/db.py 统一设 per-thread + WAL + foreign_keys=ON + busy_timeout=5000；
--     PRAGMA 必须在事务外设置（WAL 切换在事务内会静默失败）。
--   * 禁 INSERT OR REPLACE；子表重跑一律 ON CONFLICT DO UPDATE（R6）。
--   * 所有写入走参数化 SQL，禁止字符串拼值。
-- 幂等：全部 CREATE ... IF NOT EXISTS，可重复 executescript。

CREATE TABLE IF NOT EXISTS problems (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  source TEXT,
  source_id TEXT,
  statement_md TEXT NOT NULL,
  -- 手工导入去重：NULL 索引拦不住，故用 STORED 生成列 + UNIQUE
  statement_key TEXT GENERATED ALWAYS AS (substr(statement_md, 1, 500)) STORED,
  spec_json TEXT,
  tags_json TEXT,
  tier TEXT,                                              -- A/B/C
  status TEXT NOT NULL DEFAULT 'draft',                   -- draft/preparing/prepared/failed
  failed_stage TEXT,
  lang_available TEXT,
  analysis_json TEXT,                                     -- LLM 题解（结构化）
  data_plan_json TEXT,                                    -- LLM 数据计划 + 实际产出（分组/档位/条数）
  algorithm_tags TEXT,                                    -- 受控词表标签 JSON
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(statement_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_src
  ON problems(source, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS test_cases (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  layer TEXT NOT NULL,        -- sample/boundary/exhaustive/stress/adversarial
  ordinal INTEGER NOT NULL,   -- 重跑不乱序
  input_data TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  expected_output TEXT,
  source_of_truth TEXT CHECK(source_of_truth IN ('oracle','dual_impl','reference','sample','hand')),
  trust_level TEXT CHECK(trust_level IN ('oracle','cross_verified','reference_only','conflict')),
  seed INTEGER,
  size_class TEXT,
  verified_at TEXT,
  -- v2：数据来源（generated=生成器产出；user=用户合并进来的）
  source TEXT NOT NULL DEFAULT 'generated',
  UNIQUE(problem_id, layer, input_hash),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);
-- 业务规则（代码层硬约束，SQL 层不重复实现）：
--   * layer='stress' 禁 source_of_truth='reference'
--   * output_type!='any_solution' 且 expected_output IS NULL 时禁 status='prepared'

CREATE TABLE IF NOT EXISTS reference_solutions (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  lang TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'reference',                 -- reference | brute
  code TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  verify_record TEXT,                                     -- JSON：{small_cases,max_n_covered,large_cases_dual,disagreements}
  artifact_version INTEGER NOT NULL DEFAULT 1,
  approach TEXT,                                          -- 解法思路（DP/贪心/线段树…）
  complexity TEXT,                                        -- 复杂度声明
  is_primary INTEGER NOT NULL DEFAULT 0,                  -- 期望输出采用的解
  samples_expected INTEGER,
  verification_level TEXT DEFAULT 'cross_verified',        -- cross_verified | reference_only
  degrade_reason TEXT,
  line_notes_json TEXT,                                    -- 仅主语言 Python
  created_at TEXT,
  UNIQUE(problem_id, lang, role),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS generators (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  lang TEXT DEFAULT 'python',
  code TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  artifact_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT,
  UNIQUE(problem_id, lang),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  level INTEGER NOT NULL,
  content TEXT NOT NULL,
  quality_flag INTEGER DEFAULT 0,
  flagged_reason TEXT,
  gen_batch_id TEXT,
  approach TEXT NOT NULL DEFAULT '',                       -- 该提示对应的解法（空=旧数据/未标注）
  is_active INTEGER DEFAULT 1,
  regenerated_at TEXT,
  UNIQUE(problem_id, level, gen_batch_id),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS study_records (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  lang TEXT NOT NULL,
  code TEXT NOT NULL,
  verdict TEXT NOT NULL,                                  -- ac/wa/tle/re/ce/partial/system_error
  is_system_fault INTEGER NOT NULL DEFAULT 0,              -- 1=排除出一切学习统计
  failed_case_original_id INTEGER,
  evidence_json TEXT,
  diagnosis_json TEXT,
  saw_answer INTEGER DEFAULT 0,
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_records ON study_records(problem_id, ts DESC);

CREATE TABLE IF NOT EXISTS run_results (
  id INTEGER PRIMARY KEY,
  record_id INTEGER NOT NULL,
  case_id INTEGER NOT NULL,
  verdict TEXT,
  elapsed_ms REAL,
  output_head TEXT,
  FOREIGN KEY(record_id) REFERENCES study_records(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS problem_stage_runs (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  attempt INTEGER,
  started_at TEXT,
  finished_at TEXT,
  error_text TEXT,
  output_ref TEXT
);

CREATE TABLE IF NOT EXISTS problem_progress (
  problem_id INTEGER PRIMARY KEY,
  unlocked_hint_level INTEGER DEFAULT 0,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS problem_drafts (
  problem_id INTEGER NOT NULL,
  lang TEXT NOT NULL,
  code TEXT,
  updated_at TEXT,
  PRIMARY KEY(problem_id, lang)
);

CREATE TABLE IF NOT EXISTS similar_problems (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  platform TEXT,
  note TEXT,
  link_verified INTEGER DEFAULT 0,
  UNIQUE(problem_id, url),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER,
  stage TEXT,
  attempt INTEGER,
  ok INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  elapsed_ms REAL NOT NULL DEFAULT 0,
  tag TEXT,
  prompt_hash TEXT NOT NULL DEFAULT '',                    -- 模板指纹（sha256 前 16 位），老库走 ensure_columns 补列
  created_at TEXT,
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_problem_stage ON llm_calls(problem_id, stage);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT
);

-- ── v2 新增（数据管理）：导出历史 + 生成任务持久化 ──────────────────────────
-- 幂等追加：老库执行本段不会破坏既有数据（CREATE ... IF NOT EXISTS）。

CREATE TABLE IF NOT EXISTS export_history (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  filename TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  cases INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_export_problem ON export_history(problem_id, id DESC);

CREATE TABLE IF NOT EXISTS generation_jobs (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  status TEXT NOT NULL,           -- running | done | failed
  budget INTEGER NOT NULL DEFAULT 30,
  os_pid INTEGER,
  error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_problem ON generation_jobs(problem_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_cases_problem_layer ON test_cases(problem_id, layer);
CREATE INDEX IF NOT EXISTS idx_solutions_problem_lang ON reference_solutions(problem_id, lang);

-- B2：一个题号下的导出必须唯一（并发导出撞号由数据库兜底）
CREATE UNIQUE INDEX IF NOT EXISTS ux_export_version ON export_history(problem_id, version);
