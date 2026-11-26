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
  difficulty TEXT NOT NULL DEFAULT '',                    -- 导入时规则评估：easy / medium / hard，空=老题未评估
  exec_spec_json TEXT NOT NULL DEFAULT '',                -- 自动生成的执行规格（stdin 协议 + 转换后样例）
  statement_original TEXT NOT NULL DEFAULT '',            -- 题面被模型补全前的原文（未补全时为空）
  statement_repaired INTEGER NOT NULL DEFAULT 0,          -- 1 = 当前 statement_md 是模型补全的
  created_by TEXT NOT NULL DEFAULT '',                    -- 导入人（学号）；空 = 升级前的老题，视为公共
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
-- 每人每日额度检查走 `WHERE user=? AND created_at>=?`：没有这条索引会全表扫，
-- 175 人各跑几十题后表会到几万行，每次开任务都扫一遍
CREATE INDEX IF NOT EXISTS ix_llm_user_time ON llm_calls(user, created_at);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL DEFAULT '',                           -- 发起人：worker 据此加载他自己的 Key
  problem_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                                     -- dataset / adversarial
  status TEXT NOT NULL,                                   -- queued / running / done / failed
  stage TEXT NOT NULL DEFAULT '',
  budget INTEGER NOT NULL DEFAULT 0,
  os_pid INTEGER,
  error TEXT NOT NULL DEFAULT '',
  failed_stage TEXT NOT NULL DEFAULT '',
  result_json TEXT,
  args_json TEXT NOT NULL DEFAULT '[]',                   -- worker 参数（对抗任务的 submission-id）：排队期间也要留档
  queued_at TEXT NOT NULL DEFAULT '',                     -- 入队时间；started_at 只记真正开跑时间
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_problem ON jobs(problem_id, id DESC);

-- 队列取"同一类里最早的一条"，这条索引让取队头和算位次都不用扫全表
CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, kind, id);

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

-- ── 教学层：建模探针（2026-11-25 新增）──────────────────────────────────────
-- 四栏表探针：助手先做题建出 ①②③④ 正确表，再为每层各出一个埋错变体。
-- 一道题一份探针包（probe_packs），学生逐层找错，进度在 probe_progress。
CREATE TABLE IF NOT EXISTS probe_packs (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  state_type TEXT NOT NULL DEFAULT '',                    -- 积累/演化/匹配/搜索/维护
  state_type_note TEXT NOT NULL DEFAULT '',
  table_json TEXT NOT NULL DEFAULT '{}',                  -- 正确四栏表 {"1":…, "2":…, "3":…, "4":…}
  probes_json TEXT NOT NULL DEFAULT '[]',                 -- 四层探针（planted/error_type/correct/should_find/followups/explain）
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(problem_id),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

-- 学生 × 题目的探针进度。一次只激活一层（active_layer），一层通过再开下一层。
-- tier 是档位（detail=详查 / exempt=免检），纯查表规则维护，无训练数据。
CREATE TABLE IF NOT EXISTS probe_progress (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  user TEXT NOT NULL,
  active_layer INTEGER NOT NULL DEFAULT 1,                -- 当前要找的层；5 = 全部通过
  round INTEGER NOT NULL DEFAULT 0,                       -- 当前层已追问轮数（0=刚发问）
  tier TEXT NOT NULL DEFAULT 'detail',                    -- detail / exempt
  done INTEGER NOT NULL DEFAULT 0,                        -- 1 = 四层全过（含代填）
  passed_json TEXT NOT NULL DEFAULT '[]',                 -- [true×4]：每层是"找出"还是"代填"
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(problem_id, user),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_probe_progress_user ON probe_progress(user, updated_at);

-- 层级 × 错因 计数向量：学生档案与班级诊断报告都从这张表聚合。
CREATE TABLE IF NOT EXISTS probe_records (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  user TEXT NOT NULL,
  layer INTEGER NOT NULL,                                 -- 1~4
  error_type TEXT NOT NULL DEFAULT '',                    -- missing / redundant
  outcome TEXT NOT NULL,                                  -- found（该层通过）/ 代填（reteach）
  attempts INTEGER NOT NULL DEFAULT 1,                    -- 本层用了几轮
  tier TEXT NOT NULL DEFAULT 'detail',
  created_at TEXT NOT NULL,
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_probe_records_user ON probe_records(user, layer);
CREATE INDEX IF NOT EXISTS ix_probe_records_problem ON probe_records(problem_id);

-- 档位浮动的查表依据：每个 (user, layer) 的连续通过计数与当前档位。
-- 连续两题通过 → exempt；exempt 期间一出错 → detail 并清零。
CREATE TABLE IF NOT EXISTS probe_tiers (
  user TEXT NOT NULL,
  layer INTEGER NOT NULL,
  streak INTEGER NOT NULL DEFAULT 0,                      -- 连续"找出"次数
  tier TEXT NOT NULL DEFAULT 'detail',                    -- detail / exempt
  updated_at TEXT NOT NULL,
  PRIMARY KEY(user, layer)
);

-- ── 教学层：五步提示（2026-11-25 新增）──────────────────────────────────────
-- L0~L3 内容一次生成（hint_packs），运行期由状态机逐级放行；L4 运行期按学生代码现出。
CREATE TABLE IF NOT EXISTS hint_packs (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  tag TEXT NOT NULL DEFAULT '',                           -- 考点标签（撤除规则用它）
  attribution TEXT NOT NULL DEFAULT '',                   -- 读题/建模/复杂度/实现/策略
  pack_json TEXT NOT NULL DEFAULT '{}',                   -- L0~L3 内容（含 check 型 expected、L3 blanks）
  start_level INTEGER NOT NULL DEFAULT 0,                 -- 生成时的起点级别（同标签第 N 次上移 N-1 级）
  collapsed INTEGER NOT NULL DEFAULT 0,                   -- 1 = 塌缩梯子：只保留 L1 与 L4（同标签第 3 次起）
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(problem_id),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_hint_packs_tag ON hint_packs(tag);

-- 学生 × 题目的梯子状态。级别只由这里的状态机推进，模型无权决定给到哪一级。
CREATE TABLE IF NOT EXISTS hint_states (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  user TEXT NOT NULL,
  level INTEGER NOT NULL DEFAULT 0,                       -- 当前级别 0~4
  answered INTEGER NOT NULL DEFAULT 1,                    -- 上一问是否已答；0 = 提示按钮置灰
  done INTEGER NOT NULL DEFAULT 0,                        -- 1 = 走完梯子（L4 已给或 L3 放行后出代码环节）
  reaction TEXT NOT NULL DEFAULT '',                      -- 最近一次判断级回答的助教反应文本（AI 生成，不影响判定）；要提示时清空
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(problem_id, user),
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

-- 全程留痕：级别、触发方式、放行与否、五类归因。
CREATE TABLE IF NOT EXISTS hint_log (
  id INTEGER PRIMARY KEY,
  problem_id INTEGER NOT NULL,
  user TEXT NOT NULL,
  level INTEGER NOT NULL,                                 -- 0~4
  trigger TEXT NOT NULL DEFAULT '',                       -- self（学生主动）/ timeout / fail2
  kind TEXT NOT NULL DEFAULT '',                          -- judgment / check / blanks
  outcome TEXT NOT NULL DEFAULT '',                       -- passed / held（未放行）/ advanced / l4
  attribution TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',                        -- 学生作答摘要（截断存）
  created_at TEXT NOT NULL,
  FOREIGN KEY(problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_hint_log_user ON hint_log(user, problem_id, id DESC);

-- T4 样例解释反推验证：每题保留最新一次报告（覆盖写）
CREATE TABLE IF NOT EXISTS explain_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  problem_id INTEGER NOT NULL UNIQUE,
  user TEXT NOT NULL DEFAULT '',
  claims_json TEXT NOT NULL DEFAULT '[]',   -- 抽取的可计算断言
  report_json TEXT NOT NULL DEFAULT '[]',   -- 真跑结果：[{claim, ok, actual}]
  ok_count INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'checked',   -- checked / no_claims
  note TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT
);
