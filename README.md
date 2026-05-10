# 数据生成器 — 项目说明文档

> **版本**：1.0.0　|　**技术栈**：Python + PySide6 + DeepSeek / OpenAI 兼容 API　|　**定位**：算法竞赛测试数据生成 & AI 辅助解题平台
*最后修改时间：2026-04-22*

---

## 目录

1. [项目概述](#1-项目概述)
2. [目录结构](#2-目录结构)
3. [核心数据模型](#3-核心数据模型)
4. [主界面与 UI 层](#4-主界面与-ui-层)
5. [变量配置系统](#5-变量配置系统)
6. [数据生成引擎](#6-数据生成引擎)
7. [AI 服务层](#7-ai-服务层)
8. [题目工作台](#8-题目工作台)
9. [Isolated Agent 引擎（核心）](#9-isolated-agent-引擎核心)
10. [双算法对拍验证（Verifier）](#10-双算法对拍验证verifier)
11. [分值分配器](#11-分值分配器)
12. [更新服务](#12-更新服务)
13. [打包与部署](#13-打包与部署)
14. [日志系统](#14-日志系统)
15. [完整数据流](#15-完整数据流)
16. [关键设计决策](#16-关键设计决策)
17. [扩展与接入指南](#17-扩展与接入指南)

---

## 1. 项目概述

**数据生成器**是一款面向 OI / ACM 算法竞赛出题场景的桌面工具，核心功能分为两大部分：

| 功能域 | 描述 |
|--------|------|
| **测试数据生成** | 通过可视化变量配置，批量生成符合题目约束的测试数据（`stdin`），支持范围随机、枚举选择、字符集、自定义代码四种数据源，输出标准的 `problem_set` 结构 |
| **AI 辅助解题** | 调用大语言模型自动生成解题代码（`solve` 函数），配合自治 Agent 引擎进行多轮自动修复，并可选启用双算法对拍验证，提升代码正确性置信度 |

**目标用户**：OI / ACM 赛事出题人、在线判题平台（OJ）管理员、算法训练营教师。

---

## 2. 目录结构

```
数据生成器/
├── main.py                        # 程序入口，启动 PySide6 Application
├── core/
│   ├── __init__.py
│   └── models.py                  # 核心数据模型 VariableConfig
├── ui/
│   ├── main_window.py             # 主窗口
│   ├── styles/                    # QSS 样式表
│   └── widgets/
│       ├── variables_panel.py     # 左侧变量配置面板
│       ├── generation_panel.py    # 右侧生成参数面板
│       ├── variable_row.py        # 单条变量配置行组件
│       ├── preview_dialog.py      # 数据预览弹窗
│       ├── problem_workbench_dialog.py  # 题目工作台弹窗
│       ├── code_editor_dialog.py  # 代码编辑弹窗（用于变量代码源）
│       ├── test_run_dialog.py     # 测试运行弹窗
│       ├── ai/                    # AI 相关 UI 组件
│       │   ├── api_key_dialog.py              # API Key 设置对话框
│       │   ├── deepseek_refine_dialog.py      # AI 解题/精炼对话框
│       │   ├── autonomous_fix_handler/        # 自治修复 Handler（UI 适配层）
│       │   ├── prompt_builder/                # Prompt 构建（UI 层）
│       │   ├── stream_render_handler/         # 流式输出渲染 Handler
│       │   └── ui_builder/                    # 对话框 UI 构建工具
│       ├── code_editor/           # 代码编辑器组件
│       │   ├── code_editor.py                 # 带行号的代码编辑器
│       │   ├── python_syntax_highlighter.py   # Python 语法高亮
│       │   ├── python_code_completer.py       # 代码补全
│       │   └── completion_parentheses.py      # 括号自动补全
│       ├── test_run/              # 测试运行子系统
│       │   ├── run_controller.py              # 运行状态控制器
│       │   ├── result_cache.py                # 运行结果缓存
│       │   ├── result_presenter.py            # 结果展示
│       │   ├── feedback_flow.py               # 反馈流程
│       │   ├── stdin_history.py               # stdin 历史记录
│       │   └── view.py                        # 测试运行视图
│       └── common/                # 通用 UI 组件
│           ├── update_dialog.py               # 更新提示对话框
│           ├── message_box.py                 # 消息框
│           └── clickable_line_edit.py         # 可点击输入框
├── services/
│   ├── ai/                        # AI 调用层
│   │   ├── openai_compat_http.py  # OpenAI 兼容 HTTP 客户端（流式）
│   │   ├── deepseekModel.py       # DeepSeek 模型列表查询
│   │   ├── deepseek_config.py     # DeepSeek 配置（API Key / BaseURL / Model）
│   │   ├── deepseek_dispatch.py   # DeepSeek 分发器
│   │   ├── default_dispatch.py    # 默认分发器（统一入口）
│   │   ├── dispatch_api.py        # 分发 API 接口定义
│   │   ├── secure_storage.py      # API Key 安全存储（基于 QSettings）
│   │   ├── workers.py             # QThread 异步 Worker（流式 / 模型列表）
│   │   ├── autonomous_fix_worker.py  # 自治修复 Worker（QThread，单Agent修复与双算法验证互斥独立）
│   │   ├── chat_session.py        # 聊天会话管理
│   │   └── errors.py              # AI 服务异常定义
│   ├── prompts/
│   │   ├── prompt_builders.py     # Prompt 模板构建（数据生成 / 解题 / Agent 系统提示）
│   │   └── code_templates.py      # 代码模板（generate_data / solve 骨架）
│   ├── preview/
│   │   ├── preview_sample.py      # 核心数据生成引擎（含沙箱执行）
│   │   ├── preview_policy.py      # 预览策略（行数 / 字符限制）
│   │   └── output_cleanup.py      # 输出目录清理工具
│   ├── isolated_agent/            # 自治 Agent 引擎（核心模块，详见第 9 节）
│   │   ├── facade.py              # 单一对外入口
│   │   ├── contracts/             # 协议常量与类型
│   │   ├── session/               # 会话状态
│   │   ├── runtime/               # 沙箱执行器与安全守卫
│   │   ├── controller/            # 自治循环控制器与动作分发
│   │   ├── verifier/              # 双算法对拍验证层
│   │   │   ├── analyzer.py         # 阶段一：题目分析与算法提取
│   │   │   ├── cross_runner.py     # 阶段二：双代码对拍与交叉反馈
│   │   │   ├── data_pool.py        # 数据池过滤层（stdin 长度 + 数量上限）
│   │   │   ├── verdict.py          # 验证结果数据类
│   │   │   └── __init__.py
│   ├── score_distributor.py       # 测试点分值分配
│   ├── solve_batch_runner.py      # 批量解题运行器
│   ├── test_zip_exporter.py       # 测试包 ZIP 导出器
│   └── update/
│       ├── update_config.py       # 版本与更新配置
│       └── update_service.py      # 更新检测服务
├── utils/
│   ├── ai_text_utils.py           # AI 输出文本处理工具
│   └── markdown_renderer.py       # Markdown 渲染器
├── File/
│   ├── version.json               # 本地版本文件
│   ├── API_Help_text.txt          # 界面内 API 帮助文本
│   └── python_keywords.json       # Python 关键字列表（代码补全用）
├── logs/                          # 运行日志目录（自动创建）
│   ├── autonomous_trace.jsonl     # 自治 Agent 每步追踪日志
│   └── verifier_trace.jsonl       # 对拍验证追踪日志
├── assets/                        # 静态资源（图标等）
├── build_exe.bat                  # PyInstaller 打包脚本
├── upload_version.bat / .ps1      # 版本发布脚本
├── 数据生成器.spec                # PyInstaller spec 文件
└── plant.md                       # 开发规划文档（verifier 设计方案）
```

---

## 3. 核心数据模型

### `VariableConfig`（`core/models.py`）

描述一个变量的完整配置，是整个数据生成管道的基本单元。

```python
@dataclass
class VariableConfig:
    name: str            # 变量名（同时作为 stdin 中的字段标识）
    var_type: str        # 类型：'integer' | 'float' | 'string'
    source_type: str     # 数据源：'range' | 'list' | 'charset' | 'code'
    params: Dict[str, Any]  # 数据源参数（见下表）
    separator: str       # 输出分隔符：'' | ' ' | '\n' | '\t' | ',' | ';'
    loop_count: int      # 循环生成次数（可引用其他变量表达式，如 "n"）
    dependencies: List[str]  # 依赖的变量名列表（用于构建依赖图）
```

**`params` 字段详解**：

| `source_type` | `params` 字段 | 说明 |
|---------------|---------------|------|
| `range` | `min`, `max` | 数值范围，支持表达式（如 `"n*2"`） |
| `list` | `items` | 逗号分隔的枚举值，如 `"yes,no,maybe"` |
| `charset` | `charset`, `length` | 字符集（`a-z` / `A-Z` / `0-9` / `a-zA-Z0-9`），长度支持范围表达式 |
| `code` | `code` | Python 代码，可定义 `generate_data(problem) -> dict` 函数，或单行表达式 |

### `problem_set` 结构

生成结果的标准输出格式：

```json
{
  "count": 10,
  "title": "题目名称（从 generate_data 返回值提取）",
  "generate_data_code": "...",
  "cases": [
    {
      "stdin": "3\n1 2 3\n",
      "stdout": "",
      "meta": { "score": 5 },
      "vars": { "n": "3", "arr": "1 2 3" },
      "score": 5
    }
  ]
}
```

---

## 4. 主界面与 UI 层

### 主窗口（`ui/main_window.py`）

主窗口采用左右分栏布局（`QSplitter`，默认比例 3:2）：

- **左侧**：`VariablesPanel`——变量配置列表，管理多个 `VariableRowWidget`
- **右侧**：`GenerationPanel`——生成参数设置与操作触发

**关键槽函数**：

| 槽 | 触发条件 | 行为 |
|----|----------|------|
| `on_preview_clicked` | 点击"预览" | 打开 `PreviewDialog`，展示前 N 条预览数据（轻量限制） |
| `on_generate_clicked` | 点击"生成" | 调用完整生成管道，打开 `ProblemWorkbenchDialog` |
| `_check_updates` | 启动后 400ms | 检查 GitHub 更新源，若有新版本弹出 `UpdateDialog` |

**设置持久化**：通过 `QSettings("DataGenerator", "Config")` 持久化 `GenerationPanel` 中的用户配置（数量、分值等）。

### `GenerationPanel`（右侧面板）

提供以下配置项：

| 配置项 | 说明 |
|--------|------|
| **数量** | 要生成的测试点数量（默认 10） |
| **禁止重复** | 勾选后对生成的 `stdin` 去重，最大尝试次数 = `min(count * 500, 50000)` |
| **启用分值** | 勾选后按权重分配总分到各测试点 |
| **总分** | 整个题目的总分值（分值分配时使用） |
| **输出目录** | 生成文件的保存路径 |
| **清理旧文件** | 按时间范围删除输出目录中的旧文件 |

### `VariableRowWidget`（变量行组件）

每个变量行包含：

- **变量名**输入框
- **类型选择**下拉框（整数 / 浮点数 / 字符）
- **数据源选择**下拉框（数据范围 / 列表选择 / 字符集合 / 来自代码）
- **动态参数区**（`QStackedWidget`）：根据数据源类型切换显示不同参数输入控件
- **分隔符**下拉框
- **循环次数**输入（支持引用其他变量名表达式）
- **删除按钮**（发射 `delete_requested` 信号）

---

## 5. 变量配置系统

### 数据源类型详解

#### `range`（数值范围）
- `min` / `max` 支持引用已定义变量名（如将 `max` 设为 `n`，则每次生成时动态解析当前 `n` 的值）
- 内部使用 `_resolve_number_expr()` 通过安全 AST eval 解析表达式

#### `list`（枚举列表）
- 逗号分隔的字符串，随机选一项返回
- 示例：`items = "Alice,Bob,Charlie"`

#### `charset`（字符集）
- 支持预定义字符集：`a-z`、`A-Z`、`0-9`、`a-zA-Z0-9`
- `length` 支持范围（如 `"5,10"` 表示随机 5~10 个字符）

#### `code`（自定义代码）
- **模式一（表达式）**：单行表达式，通过安全 `eval` 执行，可访问已定义变量
  - 示例：`random.randint(1, n)`
- **模式二（函数）**：定义 `generate_data(problem: dict) -> dict` 函数
  - 返回格式：`{'stdin': str, 'stdout': str, 'meta': dict, 'title': str}`
  - 此时该变量的值即为整个测试点的 `stdin`（最强模式）

### 安全沙箱（`preview_sample.py`）

代码模式下的执行环境受严格限制：

- **允许导入**：`random`, `math`, `itertools`, `string`
- **允许调用**：Python 内置安全函数（`abs`, `int`, `len`, `range`, `sorted` 等 ~20 个）
- **禁止**：`open`, `exec`, `eval`, `compile`, `input`, `__import__`（除白名单外）、所有下划线属性、`import os` 等系统模块
- 实现方式：AST 静态分析（`_validate_safe_expr_ast` / `_validate_safe_exec_ast`）+ 受限 `__builtins__`

### 变量依赖与执行顺序

多个变量按照在列表中的顺序依次生成，后定义的变量可以在表达式中引用前面变量的值（通过共享 `env` 字典）。`dependencies` 字段目前用于记录依赖关系，未来可用于构建依赖图实现自动排序。

---

## 6. 数据生成引擎

### 核心函数（`services/preview/preview_sample.py`）

| 函数 | 用途 |
|------|------|
| `build_preview_samples()` | 预览模式，轻量限制（单元格最大字符数、行数上限），用于 `PreviewDialog` |
| `build_generated_samples()` | 完整生成模式，放宽限制（2,000,000 字符/单元格，10,000 条/循环） |
| `build_problem_set()` | 最终生成，返回完整 `problem_set` 结构，包含 `cases` 列表 |

### 生成流程（`_build_cases`）

```
对每个测试点（case）：
  for 每个 VariableConfig cfg：
    1. 解析 loop_count（支持变量引用表达式）
    2. 循环 loop_count 次调用 _generate_value(cfg, env)
    3. 将生成值追加到 stdin_parts
    4. 将变量值写入 vars_dict 和 env（供后续变量引用）
    5. 若 cfg 是 code 类型且返回 dict，提取 meta/stdout/title 等附加信息
  拼接所有 stdin_parts，确保末尾有 '\n'
  返回 case = { stdin, stdout, meta, vars }
```

### 去重机制

去重模式（`no_duplicates=True`）：以 `stdin` 字符串为 key，使用 `set` 去重，最大尝试次数 = `min(count * 500, 50000)`。若尝试超过上限仍未凑足数量，则用重复数据填充（保证数量不减）。

---

## 7. AI 服务层

### HTTP 客户端（`services/ai/openai_compat_http.py`）

兼容 OpenAI Chat Completions API，支持：
- **流式输出**：`stream_chat_completion()` 逐 chunk 返回
- **同步调用**：封装非流式请求
- 超时默认 90 秒

### 模型配置（`services/ai/deepseek_config.py`）

| 配置项 | 默认值 | 存储键 |
|--------|--------|--------|
| API Key | — | `deepseek/api_key` |
| Base URL | `https://api.deepseek.com` | `deepseek/base_url` |
| Model | `deepseek-reasoner` | `deepseek/model` |

配置通过 `SecureSettingsStore`（基于 `QSettings`，组织名 `PyQtApp`，应用名 `DataGenerator`）持久化存储。

### 异步 Worker（`services/ai/workers.py`）

基于 `QThread`，不阻塞 UI：

- **`StreamWorker`**：流式请求，每个 chunk 发射 `delta(request_id, reasoning, content)` 信号，完成后发射 `done`，错误发射 `err`
- **`ModelsWorker`**：异步拉取可用模型列表

### Prompt 模板（`services/prompts/prompt_builders.py`）

| 函数 | 用途 |
|------|------|
| `get_generate_data_system_prompt()` | 数据生成专家角色提示词（系统级，无评分规则） |
| `get_generate_data_system_prompt_with_scoring()` | 数据生成专家角色提示词（系统级，带评分规则） |
| `build_generate_data_prompt(problem)` | 数据生成任务用户提示词（仅题目约束+补充要求） |
| `get_solve_system_prompt()` | 解题专家角色提示词（系统级） |
| `build_solve_prompt(spec, stdin)` | 解题任务用户提示词（仅题目规范+测试输入） |
| `get_agent_system_prompt()` | 自治 Agent 完整系统提示词（定义 5 种动作协议） |

**分层设计**：
- **System Prompt**：角色定义（+ 评分规则动态注入）
- **User Prompt**：具体任务数据（题目描述、约束、补充要求）

---

## 8. 题目工作台

### `ProblemWorkbenchDialog`

点击"生成"后弹出的综合工作台，包含：

1. **数据预览区**：以 JSON 格式展示 `problem_set`（智能截断，单个 case 超过 80,000 字符时截断）
2. **代码编辑器**：嵌入 `CodeEditorContainer`，支持 Python 语法高亮、代码补全、括号自动配对
3. **解题操作**：
   - **AI 生成解题代码**：调用 `build_solve_prompt` + LLM，生成 `solve(problem, data) -> dict` 函数
   - **AI 精炼对话**（`DeepSeekRefineDialog`）：打开独立对话框，支持流式输出、自治修复、双算法对拍验证
4. **测试运行**（`TestRunDialog`）：对每个 case 执行 `solve` 函数，展示 OK / RE / Timeout 状态，带结果缓存
5. **导出**：将测试点导出为 ZIP 包

### `DeepSeekRefineDialog`（AI 解题对话框）

核心功能：
- **普通模式**：单次 LLM 调用，流式展示思维链（`reasoning_content`）和代码（`content`）
- **自治修复模式**：启动 `AutonomousFixWorker`（QThread），驱动 Isolated Agent 引擎多轮迭代，UI 实时更新每步动作日志
- **双算法对拍验证**：与自治修复**互斥独立**，勾选后直接调用 `verify_with_cross_agent`，不再事先运行 `solve_with_agent`，需手动开启

---

## 9. Isolated Agent 引擎（核心）

> 路径：`services/isolated_agent/`
> 这是项目技术含量最高的模块，实现了一个完整的自治代码修复 Agent。

### 整体架构（7 层）

```
facade.py          ← 外部调用唯一入口
  └─ controller/
       ├─ api.py               ← create_session / run_autonomous_session
       ├─ autonomous_loop.py   ← 自治循环主驱动
       ├─ dispatcher.py        ← 动作分发路由
       └─ context_tools.py     ← 上下文组装工具
  └─ session/
       └─ state.py             ← 会话状态容器
  └─ runtime/
       ├─ executor.py          ← 子进程沙箱执行器
       └─ guard.py             ← AST 静态安全守卫
  └─ contracts/
       ├─ protocol.py          ← 协议常量词表
       ├─ validator.py         ← 动作 JSON 校验
       └─ errors.py            ← 异常定义
```

### 对外入口（`facade.py`）

#### `solve_with_agent()`

```python
result = solve_with_agent(
    problem=problem_dict,          # 题目描述字典
    candidate_code=initial_code,   # 初始候选代码（可为骨架）
    llm_caller=llm_caller_func,    # LLM 调用函数：(messages) -> str
    runtime_context={              # 运行上下文
        "stdin": "3\n1 2 3\n",     # 测试输入
        "expected_stdout": "6\n",  # 期望输出（可为 None）
        "timeout_sec": 3.0,        # 执行超时秒数
        "checker": None,           # 自定义校验函数（可选）
        "agent_system_prompt": "", # 自定义系统提示词（可选）
    },
    max_attempts=15,               # 最大自治轮数
    context_rounds=15,             # 保留的历史事件数
    session_id="my-session",       # 会话 ID
    on_step_callback=callback,     # 每步回调（可选，用于 UI 更新）
    should_stop=lambda: False,     # 中断检测（可选）
)
# 返回：{ "passed": bool, "final_code": str, "attempts": int, "summary": dict }
```

### 会话状态（`session/state.py`）

`AgentSessionState` 维护单会话的全部状态：

| 字段 | 说明 |
|------|------|
| `current_code` | 当前权威代码（每次 modify 后更新） |
| `version` | 代码版本号（从 1 开始递增） |
| `code_hash` | SHA256 哈希，用于检测代码是否真正修改 |
| `recent_events` | 最近 N 条动作摘要（滚动窗口，默认 15 条） |
| `last_passed_version` | 最近一次 run=right 时的版本号 |
| `last_self_test_version` | 最近一次 self_test 时的版本号 |
| `last_self_test_all_stable` | 最近一次 self_test 是否全部稳定 |
| `runtime_context` | 运行上下文（stdin / checker / timeout 等） |

**finalize 前置条件**（`can_finalize()` 方法）：
- `last_passed_version == version`（当前版本已通过 run）
- `last_self_test_version == version`（当前版本已完成 self_test）
- `last_self_test_all_stable == True`（self_test 全部稳定）

三条同时满足才允许 finalize，否则返回 `protocol_error`。

### Agent 协议（5 种动作）

Agent 每轮只能输出一个合法 JSON 动作，协议定义在 `contracts/protocol.py`：

| 动作 | JSON 格式 | 说明 |
|------|-----------|------|
| `read` | `{"action": "read", "target": "code\|problem\|all\|data_sample"}` | 读取当前代码/题目/全部/数据样本 |
| `run` | `{"action": "run", "use_current": true}` 或 `{"action": "run", "code": "..."}` | 执行代码，`use_current` 优先 |
| `modify` | `{"action": "modify", "code": "..."}` | 覆盖更新当前代码（`code` 字段必填） |
| `self_test` | `{"action": "self_test", "cases": [...]}` | 自测 1~5 条用例稳定性 |
| `finalize` | `{"action": "finalize"}` | 确认提交，触发 finalize 前置检查 |

**运行结果类型**：

| 类型 | 含义 |
|------|------|
| `right` | 代码运行通过（stdout 匹配期望或无期望时直接通过） |
| `wrong` | 代码运行但输出不符合期望 |
| `runtime_error` | 运行时异常或超时（含 `time` 字段时为超时） |
| `grammatical_error` | 语法/编译错误 |
| `system_error` | 安全守卫拦截（危险代码） |

### 自治循环（`controller/autonomous_loop.py`）

```
初始化 LoopConfig（max_attempts=15, max_protocol_errors=3, max_no_progress_rounds=5）

for attempt in range(1, max_attempts+1):
    1. 检查中断信号（should_stop）
    2. 组装上下文消息（system_prompt + 最近 N 条事件摘要）
    3. 调用 llm_caller(messages) → 获取 LLM 输出
    4. 解析 LLM 输出为 action_payload（JSON）
       ├─ 解析失败 → protocol_error++，若达上限则退出
    5. dispatcher.dispatch(action_payload, state) → 执行动作
    6. 记录 step_info，触发 on_step_callback
    7. 若 result_type == finalize_ok → 返回 passed=True
    8. 若连续 5 轮无代码变化 → 退出（no_progress_limit_reached）

返回 passed=False + final_code + summary
```

**防死循环机制**：
- `max_protocol_errors=3`：连续 3 次协议错误则退出
- `max_no_progress_rounds=5`：连续 5 轮 run/modify/self_test 而代码哈希不变则退出
- `max_attempts=15`：硬上限

### 沙箱执行器（`runtime/executor.py`）

代码执行流程：
1. **AST 静态检查**（`guard.py`）→ 命中危险规则立即返回 `system_error`
2. **语法编译检查** → 语法错误返回 `grammatical_error`
3. **子进程执行**：
   - 写临时 `.py` 文件（`_build_runner_script` 包装 `solve` 函数调用）
   - `subprocess.run([sys.executable, "-I", script_path], ...)`
   - 通过环境变量 `ISOLATED_AGENT_CASE`（JSON）传递 stdin
   - 超时默认 3 秒，输出截断默认 12,000 字符
   - 执行后删除临时文件
4. **结果映射** → 调用 checker 函数判断 right / wrong

### 安全守卫（`runtime/guard.py`）

AST 静态分析，拦截规则：

| 规则 | 触发条件 |
|------|----------|
| `blocked_import` | 导入黑名单模块（`os`, `subprocess`, `socket`, `pathlib`, `shutil`, `ctypes`, `requests`, `urllib`, `http`, `ftplib`, `telnetlib`, `pickle`, `marshal`） |
| `blocked_import_from` | `from 黑名单模块 import ...` |
| `blocked_call` | 调用危险函数（`open`, `exec`, `eval`, `compile`, `input`, `__import__`, `breakpoint`） |
| `blocked_attr_call` | 危险模块属性调用（如 `os.system()`） |
| `blocked_dunder_attr` | 访问双下划线属性（如 `__class__`） |

守卫只做高确定性拦截，不过度拦截正常算法代码。

### 每步追踪日志

自治循环每个关键节点追加 JSONL 到 `logs/autonomous_trace.jsonl`：

```json
{"ts": "2026-04-21T10:00:00", "event": "attempt_begin", "session_id": "...", "version": 2, "code_hash": "sha256:...", "payload": {...}}
```

事件类型：`loop_start` / `attempt_begin` / `llm_output` / `action_payload` / `dispatch_result` / `loop_success` / `loop_failed` 等。

---

## 10. 双算法对拍验证（Verifier）

> 路径：`services/isolated_agent/verifier/`
> 对外入口：`facade.verify_with_cross_agent()`

**注意**：该功能**默认关闭**（`enable_cross_validation=False`），需显式开启。

### 完整流程

```
verify_with_cross_agent(problem, candidate_code, test_cases, llm_caller_factory)
         │
         ▼
  【阶段一】analyzer.analyze_problem()
         │  调用 1 次 LLM，返回 AnalysisResult：
         │  - can_brute_force: bool
         │  - max_n_for_brute: int | None
         │  - output_type: "single_value" | "list" | "any_solution"
         │  - algorithms: [AlgorithmInfo, ...]（至少 2 条）
         │  - confidence: "high" | "medium" | "low"
         │
         ▼
  【路由决策】
  ├─ output_type == any_solution → strategy=skip（构造题，输出不唯一）
  ├─ confidence == low → strategy=skip（算法路径不清晰）
  └─ else → 进入阶段二
         │
         ▼
  【阶段二】cross_runner.run_cross_validation()
         │
         ├─ data_pool.prepare_cross_validation_pool()
         │      按 stdin 长度过滤（默认 ≤3000 字符）
         │      按池子上限随机采样（默认 ≤50 条）
         │      过滤后为空 → strategy=skip（data_pool_empty）
         │
         ├─ generate_dual_codes()（并发 2 个独立 Agent）
         │      ThreadPoolExecutor(max_workers=2)
         │      Agent-A：指定算法路径 A（暴力解 或 算法A）
         │      Agent-B：指定算法路径 B（优化解 或 算法B）
         │      每个 Agent 内部运行完整 solve_with_agent 自治循环
         │
         ├─ 两份代码在全部 test_cases 上运行，比对 stdout
         │      can_brute_force=True：Agent-A 超时时跳过该用例（不终止）
         │      can_brute_force=False：任一超时立即终止（optimized_timeout）
         │
         ├─ 若全部匹配 → verification_passed=True，返回 VerifyVerdict
         │
         └─ 若存在不匹配 → 交叉反馈重试
                将首个不匹配用例的 stdin/out_a/out_b 注入到 problem['cross_feedback']
                Agent-A 和 Agent-B 各用最多 5 轮额外预算重试修复
                再次对拍：匹配 → passed，不匹配 → mismatch_after_retry
         │
         ▼
  【结果】VerifyVerdict
    - verification_passed: bool
    - final_code: str（优先返回优化代码）
    - brute_force_code: str
    - optimized_code: str
    - strategy_used: "brute_force" | "cross_algo" | "skip"
    - skip_reason: str
    - mismatch_cases: List[dict]（不一致用例详情）
    - summary: dict（统计信息）
```

### 降级规则（skip 策略）

| 触发条件 | skip_reason |
|----------|-------------|
| 功能未开启 | `disabled` |
| analyzer LLM 调用失败 | `analyzer_error` |
| output_type == any_solution | `output_not_unique` |
| confidence == low | `low_confidence` |
| 双代码生成失败 | `dual_generation_failed` |
| test_cases 为空 | `empty_test_cases` |
| 数据池过滤后为空（stdin 均超长） | `data_pool_empty` |
| 优化代码执行超时 | `optimized_timeout` |
| 两轮对拍后仍不匹配 | `mismatch_after_retry` |

### 对拍验证追踪日志

关键节点追加 JSONL 到 `logs/verifier_trace.jsonl`：

```json
{"ts": "2026-04-21T10:05:00", "event": "cross_validation_start", "payload": {"strategy": "cross_algo", ...}}
```

事件类型：`cross_validation_start` / `data_pool_filtered` / `dual_codes_generated` / `first_compare_done` / `retry_done` / `second_compare_done` / `cross_validation_success` / `cross_validation_degraded`。

### 数据池过滤层（`data_pool.py`）

> 在双代码生成前，对 test_cases 进行两阶段过滤，防止大型输入填满 LLM 上下文或导致对拍耗时过长。

**过滤规则**：

| 阶段 | 规则 | 默认值 | 行为 |
|------|------|--------|------|
| 按 stdin 长度 | 单条 stdin 字符数超过阈值 | 3000 字符 | 剔除 |
| 按池子上限 | 池子条数超过上限 | 50 条 | 随机采样（`random.sample`） |

**函数签名**：

```python
def prepare_cross_validation_pool(
    test_cases: list,
    *,
    max_stdin_chars: int = 3000,
    max_pool_size: int = 50,
    seed: Optional[int] = None,
) -> PooledCases:
```

**`PooledCases` 结构**：

```python
@dataclass
class PooledCases:
    cases: List[Dict[str, str]]       # 过滤后的用例列表
    total_original: int                # 原始用例总数
    total_filtered_by_size: int        # 因 stdin 过长被过滤的数量
    total_dropped_by_cap: int          # 因池子上限被丢弃的数量
    total_retained: int                # 最终保留数量 (= len(cases))
    is_empty: bool                     # 过滤后是否为空
```

**降级条件**：过滤后 `is_empty == True` → `skip_reason = "data_pool_empty"`。

**设计要点**：
- 过滤层位于 `cross_runner.run_cross_validation()` 内部，对 `facade.verify_with_cross_agent()` 调用方透明。
- `SolveBatchRunner` 不受影响，批量测试仍使用全量数据。
- 可以通过 `seed` 参数保证采样结果可复现。

---

## 11. 分值分配器

**文件**：`services/score_distributor.py`

函数签名：

```python
def distribute_scores(total_score: int, cases: List[Dict]) -> List[int]
```

**分配逻辑**：

1. **加权分配**：若任意 case 的 `vars` 中包含 `score` 或 `得分` 键，则使用加权分配（最大余数法，保证总和精确等于 `total_score`）
2. **均匀分配**：否则均分，余数分配给前几个 case（保证总和精确）

加权模式下，负权重自动当作 0 处理。

---

## 12. 更新服务

**配置**（`services/update/update_config.py`）：

```python
CURRENT_VERSION = "1.0.0"
UPDATE_FEED_URL = "https://raw.githubusercontent.com/RuiHe-cell/data-generator-update-feed/main/version.json"
LOCAL_VERSION_FILE = r"File\version.json"
UPDATE_TIMEOUT_SECONDS = 6
UPDATE_CHECK_INTERVAL_SECONDS = 21600  # 6 小时
```

**流程**：
1. 启动后 400ms 检查（`QTimer.singleShot`），避免影响启动速度
2. 读取 `QSettings` 中的 `update/last_checked` 时间戳，若距上次检查不足 6 小时则跳过
3. HTTP GET `UPDATE_FEED_URL`，解析 `{ version, download_url, notes, published_at }`
4. 语义化版本比较（按点分 int 元组），若远端 > 本地则弹出 `UpdateDialog`
5. 用户点击"下载"则通过 `QDesktopServices.openUrl` 打开下载链接
6. 检查完成后写入时间戳，防止频繁弹窗

本地优先使用 `File/version.json` 中的版本号，文件不存在时回退到 `update_config.CURRENT_VERSION`。

---

## 13. 打包与部署

### 打包（`build_exe.bat`）

使用 PyInstaller 打包为单文件可执行程序：

```
pyinstaller --name="数据生成器" --onefile --windowed --clean ...
```

- 大量排除不需要的 PySide6 子模块（Qt3D / Multimedia / WebEngine 等）以减小体积
- 显式保留：`PySide6.QtCore`, `PySide6.QtGui`, `PySide6.QtWidgets`
- 打包环境：`I:\.PythonVenv\.venv_deploy`（独立虚拟环境，避免污染开发环境）
- 输出：`dist/数据生成器.exe`（单文件，可在无 Python 环境的机器上运行）

### 运行依赖

| 依赖 | 用途 |
|------|------|
| PySide6 | GUI 框架 |
| （标准库） | `subprocess`, `ast`, `json`, `hashlib`, `tempfile`, `concurrent.futures` 等 |
| 网络 | 需要访问 AI API（DeepSeek / OpenAI 兼容端点）和 GitHub 更新源 |

**无第三方 HTTP 库依赖**：AI 客户端（`openai_compat_http.py`）和更新服务均使用 `urllib` / `http.client` 标准库实现。

---

## 14. 日志系统

| 日志文件 | 写入时机 | 格式 |
|----------|----------|------|
| `logs/autonomous_trace.jsonl` | 每次自治循环每个动作节点 | JSONL，每行一条事件 |
| `logs/verifier_trace.jsonl` | 每次对拍验证每个关键节点 | JSONL，每行一条事件 |
| `debug.log` | 项目根目录，开发调试用 | 文本日志 |

JSONL 格式：
```json
{"ts": "ISO8601时间", "event": "事件名", "session_id": "...", "version": 1, "code_hash": "sha256:...", "payload": {...}}
```

长字符串在日志中自动截断（trace 限制 6000 字符，payload 中代码截断为 6000 字符）。

---

## 15. 完整数据流

### 数据生成流

```
用户配置 VariableRowWidget（N 个变量）
        ↓
VariablesPanel.get_configs() → List[VariableConfig]
        ↓
preview_sample.build_problem_set(configs, rows, no_duplicates)
        ↓
_build_cases()：遍历每个 VariableConfig → _generate_value()
  ├─ source=range  → random.randint/uniform（支持变量表达式）
  ├─ source=list   → random.choice(items)
  ├─ source=charset→ random.choice(alphabet) * length
  └─ source=code   → safe_eval(expr) 或 safe_exec(generate_data函数)
        ↓
List[case] = [{stdin, stdout, meta, vars}, ...]
        ↓
（可选）distribute_scores(total_score, cases) → 写入 case["score"]
        ↓
problem_set = {count, title, cases, generate_data_code}
        ↓
ProblemWorkbenchDialog（展示 + 操作）
```

### AI 解题流（两条独立路径）

#### 路径一：自治修复（不勾选"双算法验证"）

```
用户在 DeepSeekRefineDialog 选择"自治修复"模式，不勾选"双算法验证"
        ↓
AutonomousFixWorker（QThread）
        ↓
facade.solve_with_agent(problem, candidate_code, llm_caller, runtime_context)
        ↓
create_session(session_id, candidate_code, ...)
        ↓
run_autonomous_loop(state, problem, llm_caller, dispatcher)
  for attempt 1..15:
    build_messages(problem, state.recent_events)
    llm_caller(messages) → action_json
    dispatcher.dispatch(action_json, state):
      read   → 返回代码/题目/数据样本
      modify → state.replace_code(new_code)
      run    → guard检查 → subprocess执行 → checker判断right/wrong
      self_test → 多用例稳定性验证
      finalize → can_finalize()? → 成功退出
    push_event(action, result_type, ...)
    on_step_callback(step_info) → 更新 UI
        ↓
{ passed, final_code, attempts, summary }
```

#### 路径二：双算法对拍验证（勾选"双算法验证"）

```
用户在 DeepSeekRefineDialog 选择"自治修复"模式，勾选"双算法验证"
        ↓
AutonomousFixWorker（QThread）
        ↓                      ← 直接进入对拍，不调用 solve_with_agent
facade.verify_with_cross_agent(problem, candidate_code, test_cases, llm_caller_factory)
        ↓
（详见第 10 节双算法对拍验证完整流程）
        ↓
{ passed, final_code, attempts=0, summary.cross_validation }
```

> **两条路径完全互斥**：`enable_cross_validation=True` 时 Worker 直接走路径二，`solve_with_agent` 不会被调用，不存在串行叠加关系。

---

## 16. 关键设计决策

| 设计点 | 决策 | 理由 |
|--------|------|------|
| **Agent 与 UI 解耦** | Agent 引擎纯 Python，不依赖 PySide6；UI 通过 `on_step_callback` 注入回调 | Agent 可独立测试和复用，UI 层替换不影响业务逻辑 |
| **子进程执行沙箱** | 用 `subprocess.run` 在独立进程执行候选代码 | 彻底隔离，主进程不受候选代码崩溃/无限循环影响 |
| **双重安全检查** | 执行前 AST 静态守卫 + 运行时子进程隔离 | AST 拦截明显危险代码，子进程提供运行时隔离兜底 |
| **短上下文设计** | Agent 上下文只包含系统提示 + 最近 N 条事件摘要，不含完整代码 | 控制 token 消耗；Agent 需要代码时主动调用 `read` |
| **对拍隔离** | 两个 Agent 完全独立会话，prompt 强制指定不同算法路径 | 防止 LLM 在同一上下文中生成结构相似的"两份"代码 |
| **降级策略** | Verifier 在任何异常情况下均降级为 skip，不抛异常 | 对拍验证是加分项，不能因验证失败阻塞主流程 |
| **版本哈希追踪** | `code_hash = SHA256(current_code)` | 检测无进展循环（modify 后哈希不变 = Agent 在原地打转） |
| **finalize 前置条件** | run=right + self_test(all_stable=True) 双重验证才允许 finalize | 防止 Agent 在未测试的情况下直接提交 |

---

## 17. 扩展与接入指南

### 接入新的 LLM 提供商

1. 在 `services/ai/` 下新增配置文件（参考 `deepseek_config.py`）
2. 实现 `llm_caller` 函数：`(messages: List[Dict]) -> str`，接收消息列表，返回模型输出字符串
3. 在 `services/ai/default_dispatch.py` 中注册新的分发入口
4. Agent 引擎无需任何修改，直接传入新的 `llm_caller` 即可

### 新增数据源类型

1. 在 `core/models.py` 的 `VariableConfig.source_type` 文档中补充新类型说明
2. 在 `ui/widgets/variable_row.py` 的 `source_map` 中添加中文名映射
3. 在 `ui/widgets/variable_row.py` 的 `QStackedWidget` 中添加对应参数面板
4. 在 `services/preview/preview_sample.py` 的 `_generate_value_for_mode()` 中添加生成逻辑

### 自定义 solve 代码校验器

```python
def my_checker(stdout_text: str, context: dict) -> bool:
    """自定义校验：忽略行末空格，仅比较实际内容"""
    expected = str(context.get("expected_stdout") or "")
    return stdout_text.strip() == expected.strip()

result = solve_with_agent(
    ...,
    runtime_context={
        "stdin": test_input,
        "expected_stdout": expected_output,
        "checker": my_checker,  # 注入自定义校验器
    }
)
```

### 使用 Isolated Agent 引擎（独立调用示例）

```python
from services.isolated_agent.facade import solve_with_agent
from services.ai.openai_compat_http import OpenAICompatHTTPClient

client = OpenAICompatHTTPClient(base_url="...", api_key="...", timeout_sec=60)

def llm_caller(messages):
    full_text = ""
    for chunk in client.stream_chat_completion(model="deepseek-reasoner", messages=messages):
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            full_text += delta.get("content") or ""
    return full_text

result = solve_with_agent(
    problem={"title": "两数之和", "constraints": "1<=n<=1000", ...},
    candidate_code="def solve(problem, data): return {'stdout': ''}",
    llm_caller=llm_caller,
    runtime_context={"stdin": "2\n1 2\n", "expected_stdout": "3\n"},
    max_attempts=15,
)
print(result["passed"])      # True/False
print(result["final_code"])  # 最终代码
print(result["attempts"])    # 消耗轮数
```

---

*文档生成时间：2026-04-21　|　基于源代码全量分析*
