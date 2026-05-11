# Isolated Agent 子系统说明与边界

本目录承载项目中的“自治修复 + 隔离执行 + 双算法验证”子系统。

> ⚠️ 维护警告：本文件与代码同步维护。若你修改了本目录下的任何 .py 文件，请务必检查并更新本文档的对应章节。

它不依赖 UI 层状态，目标是在独立上下文里完成代码读取、修改、运行、自测、提交，并向外部仅暴露稳定的结果结构。当前目录已经不只是最初的单 Agent 核心链路，还正式包含 `verifier/` 子系统，用于双算法对拍验证。

## 当前职责

- 为候选 `solve(problem, data)` 代码提供隔离式自治修复能力。
- 通过固定 JSON action 协议驱动 LLM 多轮决策。
- 通过 AST 安全守卫 + 子进程执行器，隔离运行风险。
- 通过“固定题目卡 + 动态上下文”策略控制 token 体积，避免把完整历史塞回模型。
- 在可选场景下提供双算法对拍验证，用于提高最终代码正确性置信度。
- 对外只返回最终代码、通过状态、最小摘要和验证结论，不暴露中间推理链。

## 非职责范围

- 不直接读写任何外部 UI 状态。
- 不直接 import `ui/widgets`、`test_run_dialog`、`solve_batch_runner` 等现有业务组件。
- 不维护全局会话池，不做跨会话持久化。
- 不向外部返回完整中间上下文、隐藏测试明文或完整内部推理过程。
- 不负责题目生成、Prompt 平台配置、模型分发等外围业务。

## 目录结构与角色

- `facade.py`
  对外单入口。当前暴露两个稳定接口：`solve_with_agent()` 和 `verify_with_cross_agent()`。
- `controller/`
  控制层。负责会话创建、自治循环驱动、动作分发、上下文裁剪。
- `session/`
  状态层。只保存当前权威代码、最近摘要窗口与有限版本快照，不保存完整对话历史。
- `contracts/`
  协议层。定义动作词表、结果类型、错误码、校验规则。
- `runtime/`
  运行层。负责 AST 安全守卫、语法检查、子进程执行、自测稳定性批量执行。
- `verifier/`
  验证层。负责算法分析、双 Agent 生成、交叉对拍、失败降级裁决。


## 对外接口

### `solve_with_agent()`

用途：运行单会话自治修复流程。

输入要点：

- `problem`：题目对象。
- `candidate_code`：初始候选代码。
- `llm_caller(messages)`：模型调用器。
- `runtime_context`：运行上下文，例如 `stdin`、`expected_stdout`、`checker`、`timeout_sec`、`agent_system_prompt`。
- `max_attempts`：自治轮数上限，默认 `15`。
- `context_rounds`：最近事件窗口，默认 `15`，最大 `30`。
- `on_step_callback`：每轮动作回调，供 UI 或外层观察进度。
- `should_stop`：中断信号。

返回结构：

- `session_id`
- `passed`
- `final_code`
- `attempts`
- `summary`

约束：

- facade 负责兜底异常，外部不会直接接收到内部堆栈异常。
- 输出以“最终状态”为主，不返回完整内部会话对象。

### `verify_with_cross_agent()`

用途：运行验证子系统，包括算法分析和双算法对拍。

输入要点：

- `problem`
- `candidate_code`
- `test_cases`
- `llm_caller_factory(session_tag)`
- `enable_cross_validation`
- `max_rounds`
- `on_progress`

返回结构为 `VerifyVerdict`，核心字段包括：

- `verification_passed`
- `final_code`
- `brute_force_code`
- `optimized_code`
- `strategy_used`
- `skip_reason`
- `mismatch_cases`
- `summary`

注意：

- 当 `enable_cross_validation=False` 时直接降级为 `skip`。
- verifier 是独立入口，不会改写 `solve_with_agent()` 的既有行为。

## 自治 Agent 主链路

当前自治链路如下：

1. `facade.solve_with_agent()` 创建 `AgentSessionState`。
2. `controller.api.run_autonomous_session()` 注入 `problem` 到 `runtime_context`。
3. `controller.autonomous_loop.run_autonomous_loop()` 循环调用 LLM。
4. LLM 每轮只能返回一个 JSON action。
5. `controller.dispatcher.ActionDispatcher` 校验并执行该动作。
6. `session.state.AgentSessionState` 更新权威代码、版本、摘要窗口、通过状态。
7. 满足 `finalize` 条件后返回最终代码，否则按失败原因退出。

默认停机保护：

- 最大尝试次数：`15`
- 最大协议错误次数：`3`
- 最大无进展轮数：`5`
- 最近上下文窗口：默认 `15`，最大 `30`

## 动作协议

本模块的动作协议以 [`contracts/final_code_action.md`](file:///h:/Python/PyQt5/%E6%95%B0%E6%8D%AE%E7%94%9F%E6%88%90%E5%99%A8/services/isolated_agent/contracts/final_code_action.md) 为唯一细节真源；`README.md` 只保留地图级说明。

当前固定动作集合为：

- `read`
- `run`
- `modify`
- `self_test`
- `finalize`

语义概览：

- `read`
  读取当前代码、题目、组合视图，或按需读取 `data_sample`。
- `run`
  运行当前代码或显式提交的完整代码，并返回统一的运行结果类型。
- `modify`
  用完整代码覆盖当前权威代码，同时推进 `version/code_hash`。
- `self_test`
  做 1 到 5 条样例的稳定性探测，不承担正确性判定。
- `finalize`
  提交当前版本代码，前提是当前版本已经 `run=right` 且 `self_test(all_stable=true)`。

这里仅强调两个地图级约束：

- `read(target=data_sample)` 支持从当前运行上下文提取最短样本；若样本仍过大，会返回 `data_sample_too_complex` 并终止自治修复。
- 所有动作都先经过 `contracts/validator.py` 校验，字段级要求、请求示例、响应示例、结果类型和错误语义统一以 `final_code_action.md` 为准。

## 会话状态模型

`session/state.py` 中的 `AgentSessionState` 是单会话权威状态容器，核心字段如下：

- `current_code`
- `version`
- `code_hash`
- `recent_events`
- `max_recent_events`
- `last_run_result_type`
- `last_passed_version`
- `last_self_test_version`
- `last_self_test_all_stable`
- `runtime_context`

当前设计约束：

- 只保存当前权威代码，不保存完整历史代码快照。
- 最近事件窗口仅用于动态上下文提示，不作为长期审计存储。
- `can_finalize()` 的前置条件是：
  - `last_passed_version == version`
  - `last_self_test_version == version`
  - `last_self_test_all_stable == True`

## 上下文裁剪策略

当前上下文策略已经固定为“分层上下文机制”：

- 每轮前半段固定注入题目卡，作为稳定前缀。
- 后半段仅注入动态上下文，包括最近 N 轮摘要、最近一次 `read` 预览、版本快照等可裁剪信息。

补充机制：

- 如果模型需要完整原题或代码全文，必须显式 `read(target=problem/code/all)`。
- 如果模型需要样本结构，可显式 `read(target=data_sample)`。
- 最近一次 `read` 返回的裁剪预览会被附带到后续上下文中，避免模型反复重复读取。

目标：

- 保持题目理解稳定，减少后续轮次因摘要漂移遗忘约束。
- 控制上下文长度，优先裁剪动态区而不是题目卡。
- 降低 token 成本，并尽量提高稳定前缀缓存命中。

## 运行时隔离与安全边界

运行层位于 `runtime/`，当前采用“两段式保护”：

1. AST 静态守卫
2. 隔离子进程执行

### AST 守卫

默认拦截：

- 危险模块导入：如 `os`、`subprocess`、`socket`、`pathlib`、`shutil`、`ctypes`、`requests`、`urllib`、`http`、`pickle` 等
- 危险函数调用：如 `open`、`exec`、`eval`、`compile`、`input`、`__import__`、`breakpoint`
- 危险模块属性调用：如 `os.system()`
- 双下划线属性访问：如 `__class__`

原则：

- 只做高确定性拦截。
- 不追求覆盖所有恶意路径，避免误伤正常算法代码。

### 子进程执行器

当前行为：

- 先做语法检查。
- 再把候选代码包装为临时脚本。
- 通过 `subprocess.run([sys.executable, "-I", script_path], ...)` 运行。
- 通过环境变量 `ISOLATED_AGENT_CASE` 传入 `stdin`。
- 默认超时 `3.0` 秒。
- 默认输出截断上限 `12000` 字符。
- 执行结束后删除临时脚本。

默认校验规则：

- 若 `expected_stdout is None`，则“能稳定运行”即可判定为 `right`。
- 若存在 `expected_stdout`，则按裁剪前的 `stdout.strip()` 比对。
- 可通过 `runtime_context["checker"]` 注入自定义校验器。

## 追踪日志

当前子系统会写两类 JSONL 日志：

- `logs/autonomous_trace.jsonl`
- `logs/verifier_trace.jsonl`

其中：

- `autonomous_trace.jsonl` 记录自治循环的关键节点，例如 `loop_start`、`attempt_begin`、`llm_output`、`action_payload`、`dispatch_result`、`loop_success`、`loop_failed`。
- `verifier_trace.jsonl` 记录验证流程的关键节点，例如 `cross_validation_start`、`dual_codes_generated`、`first_compare_done`、`retry_done`、`second_compare_done`、`cross_validation_success`、`cross_validation_degraded`。

日志原则：

- 长文本会被裁剪。
- 日志用于诊断，不作为公开 API。
- 日志中不应写入需要对外隐藏的测试明文细节。

## Verifier 子系统

`verifier/` 是当前 `isolated_agent` 的正式组成部分，不再是外置实验目录。

### 阶段一：算法分析

`verifier/analyzer.py` 使用一次 LLM 调用输出 `AnalysisResult`，核心字段包括：

- `can_brute_force`
- `max_n_for_brute`
- `output_type`
- `algorithms`
- `confidence`

降级规则：

- JSON 解析失败时不抛异常，而是返回 `confidence=low` 的降级结果。
- `output_type=any_solution` 时不做对拍。
- `confidence=low` 时直接跳过对拍。

### 阶段二：双代码生成与对拍

`verifier/cross_runner.py` 负责：

- 并发启动两个独立 Agent 会话生成代码。
- 为每个会话注入“指定算法路线”的系统提示词。
- 在全部测试用例上比较两份代码输出。
- 如果首次发现不一致，注入 `cross_feedback` 后给予两侧最多 `5` 轮额外修复预算。

策略：

- `brute_force`
- `cross_algo`
- `skip`

典型降级原因：

- `disabled`
- `analyzer_error`
- `cross_validation_error`
- `dual_generation_failed`
- `output_not_unique`
- `low_confidence`
- `empty_test_cases`
- `optimized_timeout`
- `mismatch_after_retry`

返回原则：

- 通过时优先返回优化代码。
- 跳过或失败时保留回退代码，不中断主流程。

## 模块级硬边界

- 不直接耦合 UI 组件或外部工作台状态。
- 不向模型直接暴露完整隐藏测试集。
- 不向外部返回完整中间事件上下文。
- 不允许协议漂移，动作和结果类型以 `contracts/` 为唯一词表来源。
- 不绕过 `runtime/` 旁路执行候选代码。
- verifier 失败时只能降级，不应破坏主修复链路。

## 文件职责速查

- `contracts/protocol.py`
  协议常量、动作名、结果类型、验证层常量。
- `contracts/errors.py`
  可序列化协议错误与错误码。
- `contracts/validator.py`
  动作负载结构校验与标准化。
- `controller/api.py`
  创建会话、单动作分发、自治会话入口。
- `controller/dispatcher.py`
  `read/run/modify/self_test/finalize` 分发与状态回写。
- `controller/autonomous_loop.py`
  自治循环、停机条件、trace 记录。
- `controller/context_tools.py`
  事件裁剪、题目卡构建、动态上下文拼装。
- `session/state.py`
  权威代码状态、最近摘要窗口与版本快照。
- `runtime/guard.py`
  AST 静态安全守卫。
- `runtime/executor.py`
  子进程执行器、默认 checker、自测稳定性执行。
- `verifier/analyzer.py`
  单次 LLM 算法分析。
- `verifier/cross_runner.py`
  双 Agent 生成、对拍、交叉反馈重试。
- `verifier/verdict.py`
  `AnalysisResult`、`VerifyVerdict` 等数据结构。
- `facade.py`
  对外稳定单入口。
- `smoke_test.py`
  最小闭环冒烟验证脚本。

## 维护约束

后续修改本目录时，应继续遵守以下原则：

- 新增行为优先落在对应分层，不把 controller、runtime、session 职责混写。
- 新增 action、结果类型或错误码时，必须同时更新 `contracts/`。
- 若调整 `finalize` 前置条件、自测规则、data sample 约束或 verifier 路由策略，应同步更新本 README。
- 若修改 trace 结构，应优先保持向后兼容，避免影响现有诊断工具。

详细动作字段与历史协议文档可参考 `contracts/final_code_action.md`，但以当前源码实现为准。
