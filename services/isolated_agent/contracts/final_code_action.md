# Final Code Action Contract

该协议用于隔离 Agent 的唯一对外入口，目标是只同步最终代码。

## Action

- 支持动作：`read` / `run` / `modify` / `self_test` / `finalize`
- 对外最终提交动作：`action = "finalize"`

## 关键约束

- 每轮只能提交一个 JSON 动作对象。
- `run/modify` 必须包含完整代码字段 `code`（字符串）。
- `self_test` 必须包含 `cases`（1-5条），每条至少包含 `stdin`，可选 `label` 与 `timeout_sec`。
- 会话维护 `version` 与 `code_hash`；`finalize` 可选携带 `code_hash` 做一致性校验。
- `finalize` 仅允许提交“当前版本且最近一次 run 判定为 right”的代码。
- `finalize` 额外要求“当前版本最近一次 self_test 结果 all_stable = true”。
- `read` 支持可选 `target`：`code`（默认）/`problem`/`all`/`data_sample`。
- 当 `target=data_sample` 时，系统返回“当前可用测试数据中最短的一条源结构”。
- 若最短样本仍超出复杂度阈值，将返回 `protocol_error(error_code=data_sample_too_complex)`，并终止自治修复，提示用户手动提供精简样例。

## Request

```json
{
  "session_id": "string",
  "problem": {
    "title": "string",
    "description": "string",
    "constraints": "string"
  },
  "seed_cases": [
    {
      "id": "string",
      "stdin": "string",
      "expected_stdout": "string"
    }
  ],
  "candidate_code": "string",
  "policy": {
    "language": "python",
    "stdlib_only": true,
    "deny_file_io": true,
    "deny_network": true,
    "timeout_sec": 3.0,
    "max_rounds": 6
  }
}
```

## Response

```json
{
  "session_id": "string",
  "action": "finalize",
  "passed": true,
  "final_code": "string",
  "summary": {
    "rounds": 4,
    "public_cases_passed": 3,
    "hidden_checks_passed": true,
    "status": "ok"
  }
}
```

## 运行动作结果类型

- `right`：运行通过
- `wrong`：结果不符合预期
- `runtime_error`：超时或运行时异常
- `grammatical_error`：语法/编译错误
- `system_error`：触发安全限制或系统拒绝执行

## Self Test 动作

- 目标：只做稳定性探测（是否崩溃/超时），不做正确性判断。
- 结果语义：
  - `ok`：程序运行完成（即使输出错误也记为 `ok`）
  - `runtime_error`：程序崩溃或触发安全拒绝
  - `timeout`：执行超时

### Self Test Request

```json
{
  "action": "self_test",
  "cases": [
    { "label": "empty", "stdin": "", "timeout_sec": 1.5 },
    { "label": "negative", "stdin": "-1\n", "timeout_sec": 2.0 }
  ]
}
```

### Self Test Response

```json
{
  "type": "self_test_ok",
  "all_stable": false,
  "results": [
    { "index": 0, "label": "empty", "status": "ok" },
    { "index": 1, "label": "negative", "status": "runtime_error" }
  ],
  "version": 7,
  "code_hash": "sha256:..."
}
```

## 安全与信息泄露限制

- 不返回隐藏测试的输入、期望输出、具体 diff。
- 失败时仅返回高层类别：`timeout` / `runtime_error` / `wrong_answer`。
- 不返回中间对话、思维链、内部修复草稿。
