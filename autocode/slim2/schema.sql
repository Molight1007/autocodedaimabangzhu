-- v2 本地版数据模型：只服务「导入切题 / 生成测试数据 / 生成对抗数据」三条链路。
-- 不建 hints / study_records / export_history / reference_solutions。
-- 全部 CREATE ... IF NOT EXISTS；时间一律 ISO 8601 UTC 字符串。

-- ── 导入 ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS import_batches (
  id INTEGER PRIMARY KEY,
  source_name TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'text',                      -- text / file
  raw_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'segmented',               -- segmented / confirmed / failed
  pdf_key TEXT NOT NULL DEFAULT '',                       -- 原始 PDF 的缓存 key（裁图用）
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_segments (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  statement_md TEXT NOT NULL,
  samples_json TEXT NOT NULL DEFAULT '[]',
  constraints_json TEXT NOT NULL DEFAULT '[]',
  judge_type TEXT NOT NULL DEFAULT 'token',
  tolerance TEXT NOT NULL DEFAULT '',
  multi_testcase INTEGER NOT NULL DEFAULT 0,
  sample_count INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'medium',
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',                 -- pending / confirmed / duplicate / skipped
  problem_id INTEGER,
  figures_json TEXT NOT NULL DEFAULT '[]',                -- 切题时按几何归属分到本题的图块
  layout_json TEXT NOT NULL DEFAULT '[]',                 -- 模型给的排版分块（相对行号），空=自动排版
  created_at TEXT NOT NULL,
  UNIQUE(batch_id, idx),
  FOREIGN KEY(batch_id) REFERENCES import_batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_segments_batch ON import_segments(batch_id, idx);

-- ── 题库 ────────────────────────────────────────────────────────────────────
-- 一个库一组题目。两类：
--   temp      —— 默认题库：所有账号可增删，最多 MAX_PROBLEMS_TOTAL 道；
--   permanent —— 永久库：只有管理员可增删，不限数量，任何操作都不自动删题。
-- 判定只在服务端做（见 banks.can_manage），前端按钮置灰只是提示。
CREATE TABLE IF NOT EXISTS banks (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'temp',                      -- temp / permanent
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

-- 默认题库必须存在：老库的题在迁移时全部回填 bank_id=1。
-- ON CONFLICT 不带目标：id 或 name 撞上都不报错（幂等，每次启动都会执行）。
INSERT INTO banks(id, name, kind, created_by, created_at)
VALUES(1, '默认题库', 'temp', '', strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS problems (
  id INTEGER PRIMARY KEY,
  bank_id INTEGER NOT NULL DEFAULT 1,                     -- 所属题库；1 = 默认题库
  title TEXT NOT NULL,
  statement_md TEXT NOT NULL,
  -- 唯一性按"库内"算：同一道题可以在默认题库和永久库各存一份。
  -- 全局唯一会让永久库在收录已有题时被静默判重、少一道。
  statement_hash TEXT NOT NULL,
  samples_json TEXT NOT NULL DEFAULT '[]',
  constraints_json TEXT NOT NULL DEFAULT '[]',
  judge_type TEXT NOT NULL DEFAULT 'token',               -- token / line / exact / float / special / interactive
  tolerance TEXT NOT NULL DEFAULT '',
  multi_testcase INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'draft',                   -- draft / generating / ready / failed
  dataset_version INTEGER NOT NULL DEFAULT 0,
  quality_json TEXT,
  layout_json TEXT NOT NULL DEFAULT '[]',                 -- 排版分块（相对行号，1=题面首行）
  exec_spec_json TEXT NOT NULL DEFAULT '',                -- 自动生成的执行规格（stdin 协议 + 转换后样例）
  statement_original TEXT NOT NULL DEFAULT '',            -- 题面被模型补全前的原文（未补全时为空）
  statement_repaired INTEGER NOT NULL DEFAULT 0,          -- 1 = 当前 statement_md 是模型补全的
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(bank_id, statement_hash)
);

CREATE INDEX IF NOT EXISTS ix_problems_bank ON problems(bank_id, id DESC);

-- 题目插图：PDF 切题时按几何位置归属，确认入库后从原 PDF 裁剪落盘
CREATE TABLE IF NOT EXISTS problem_figures (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  label TEXT NOT NULL,                                    -- fig_3（PDF 内全局图块编号）
  page INTEGER NOT NULL,                                  -- 1-based 页码
  bbox_json TEXT NOT NULL DEFAULT '[]',                   -- [x0,y0,x1,y1]，单位 pt
  path TEXT NOT NULL,                                     -- /assets/problem_<id>/fig_3.png
  width REAL NOT NULL DEFAULT 0,
  height REAL NOT NULL DEFAULT 0,
  after_line INTEGER NOT NULL DEFAULT 1,                  -- 插入位置：题面内第 N 行之后
  ordinal INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, label),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dataset_plans (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  difficulty TEXT NOT NULL DEFAULT '',
  layers_json TEXT NOT NULL DEFAULT '{}',
  coverage_json TEXT NOT NULL DEFAULT '[]',
  target_count INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, version),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS test_cases (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  layer TEXT NOT NULL,                                    -- sample / boundary / typical / stress / trap
  size_class TEXT NOT NULL DEFAULT '',
  input_data TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  expected_output TEXT NOT NULL,
  trust TEXT NOT NULL DEFAULT 'unreviewed',               -- cross_verified / partial / unreviewed / sample
  source TEXT NOT NULL DEFAULT 'generated',               -- generated / sample
  status TEXT NOT NULL DEFAULT 'active',                  -- active / rejected
  ordinal INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, version, input_hash),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_cases_problem ON test_cases(problem_id, version, status, layer);

CREATE TABLE IF NOT EXISTS case_reviews (
  id INTEGER PRIMARY KEY,
  case_id INTEGER NOT NULL,
  reviewer_model TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL,                                  -- pass / mismatch / invalid / uncertain
  reason TEXT NOT NULL DEFAULT '',
  recomputed_output TEXT,
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES test_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_reviews_case ON case_reviews(case_id);

-- ── 对抗 ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  lang TEXT NOT NULL,
  code TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  baseline_json TEXT,
  status TEXT NOT NULL DEFAULT 'new',                     -- new / ce / sample_failed / done
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, code_hash),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS adversarial_cases (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  submission_id INTEGER NOT NULL,
  input_data TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  expected_output TEXT NOT NULL,
  predicted_output TEXT NOT NULL DEFAULT '',
  actual_output TEXT NOT NULL DEFAULT '',
  run_verdict TEXT NOT NULL DEFAULT '',                   -- WA / TLE / RE / MLE
  targets TEXT NOT NULL DEFAULT '',
  why TEXT NOT NULL DEFAULT '',                           -- 推导过程（怎么算出答案）
  failure_reason TEXT NOT NULL DEFAULT '',                -- 这条输入为什么会让这份代码错（模型写的解释）
  status TEXT NOT NULL DEFAULT 'pending',                 -- active / rejected
  created_at TEXT NOT NULL,
  UNIQUE(problem_id, input_hash),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE,
  FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_adv_problem ON adversarial_cases(problem_id, status);

CREATE TABLE IF NOT EXISTS adversarial_reviews (
  id INTEGER PRIMARY KEY,
  adv_case_id INTEGER NOT NULL,
  reviewer_model TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL,                                  -- accept / reject / uncertain
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  corrected_output TEXT,
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  FOREIGN KEY(adv_case_id) REFERENCES adversarial_cases(id) ON DELETE CASCADE
);

-- ── 系统 ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL DEFAULT '',                           -- 谁花的额度（2026-11-22）
  problem_id INTEGER,
  stage TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  prompt_hash TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT NOT NULL DEFAULT '',
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  elapsed_ms REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_llm_problem ON llm_calls(problem_id, stage);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL DEFAULT '',                           -- 发起人：worker 据此加载他自己的 Key
  problem_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                                     -- dataset / adversarial
  status TEXT NOT NULL,                                   -- running / done / failed
  stage TEXT NOT NULL DEFAULT '',
  budget INTEGER NOT NULL DEFAULT 0,
  os_pid INTEGER,
  error TEXT NOT NULL DEFAULT '',
  failed_stage TEXT NOT NULL DEFAULT '',
  result_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_problem ON jobs(problem_id, id DESC);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  ip TEXT,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sessions_exp ON sessions(expires_at);

-- ── 学生账号（2026-11-22 新增）──────────────────────────────────────────────
-- 账号 = 学号；密码只存 scrypt 哈希，格式与 auth.hash_password 完全一致。
-- 明文密码不入库：初始密码由「学号 + 姓名全拼」规则推出，交付表在管理员手里。
-- 学生无改密权限；忘记密码由管理员重算哈希后 UPDATE 本表。
-- 登录：auth.authenticate() 先查本表，查不到再回落管理员凭证。
CREATE TABLE IF NOT EXISTS student_accounts (
  id INTEGER PRIMARY KEY,
  student_no TEXT NOT NULL UNIQUE,                        -- 学号，同时是登录账号
  name TEXT NOT NULL,
  class_name TEXT NOT NULL DEFAULT '',                    -- 1班 / 2班 / 3班 / 4班
  pinyin TEXT NOT NULL DEFAULT '',                        -- 姓名全拼，仅用于核对
  password_hash TEXT NOT NULL,                            -- scrypt$n$r$p$salt$hash
  status TEXT NOT NULL DEFAULT 'active',                  -- active / disabled
  last_login_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_students_class ON student_accounts(class_name, student_no);

-- ── 审计（2026-11-22 新增）──────────────────────────────────────────────────
-- 谁在什么时候删了哪道题、开了哪个生成任务。全班共用一份题库、权限又一样，
-- 出了事至少要能查是谁干的，否则只能整班连坐。
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,                                   -- login / delete_problem / start_dataset / start_adversarial
  target TEXT NOT NULL DEFAULT '',                        -- 题目 id 或批次 id
  detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);

-- ── 每人自己的模型凭据（2026-11-22 新增）────────────────────────────────────
-- 主键就是登录名：一个账号一行，别人的 Key 既读不到也用不了。
-- api_key_enc 由 autocode/slim2/secretbox.py 加密（HMAC-CTR + EtM），主密钥单独放
-- data/llm_master.key；库被单独拷走也还原不出明文。
CREATE TABLE IF NOT EXISTS user_llm_settings (
  user TEXT PRIMARY KEY,
  base_url TEXT NOT NULL DEFAULT '',                      -- 空 = 用默认 https://api.deepseek.com/v1
  model TEXT NOT NULL DEFAULT '',                         -- 空 = 用默认 deepseek-chat
  api_key_enc TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

-- ── 问题反馈（2026-11-22 新增）─────────────────────────────────────────────
-- 学生遇到问题或有改进意见时在页面里提；管理员能看到全部并回复。
-- 学生只看得到自己提的，别人提的既看不到也回不了（避免互相看到学号姓名）。
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL,                                     -- 提问题的人（学号 / 管理员名）
  name TEXT NOT NULL DEFAULT '',                          -- 显示用姓名
  kind TEXT NOT NULL DEFAULT 'bug',                       -- bug / idea / other
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  page TEXT NOT NULL DEFAULT '',                          -- 出问题的位置（题号/页面）
  status TEXT NOT NULL DEFAULT 'open',                    -- open / answered / closed
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_feedback_user ON feedback(user, id DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_status ON feedback(status, id DESC);

CREATE TABLE IF NOT EXISTS feedback_replies (
  id INTEGER PRIMARY KEY,
  feedback_id INTEGER NOT NULL,
  user TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'student',                   -- admin / student
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_feedback_replies ON feedback_replies(feedback_id, id);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT
);
