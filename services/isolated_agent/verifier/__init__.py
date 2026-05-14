"""Isolated Agent 验证层（verifier）。

核心职责：
- 在 solve_with_agent 生成候选代码后，对其做独立交叉验证。
- 通过算法分析 + 双Agent并发对拍，区分「真正通过」和「看起来像通过」。

子模块：
- analyzer     : 算法分析 Agent，输出路由决策（1次LLM调用）。
- verdict      : VerifyVerdict 数据结构，跨模块共享验证结果。
- cross_runner : 并发2个独立LLM请求 + 本地执行比对。
- agent_utils  : Agent 会话配置与 Prompt 构建（系统提示词/会话标签/反馈 payload）。
- data_pool    : 测试数据池过滤（stdin 长度 + 数量上限）。

对外入口：
- 通过 facade.py 的 verify_with_cross_agent 调用，不直接暴露子模块。
"""
