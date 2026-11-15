"""autoCode 算法题学习工具包。

分层（上游 -> 下游，禁止逆流，见 tools/check_layering.py）::

    ui/     界面层（PySide6，零业务逻辑）
    app/    编排层（problem_service / prep_service / runner_bridge）
    agent/  自治内核（loop / dispatcher / protocol / ArtifactSession）
    engine/ 确定性引擎（零 LLM：runner / sandbox / datagen / verify / judge / shrink / grammar）
    llm/    DeepSeek 客户端（chat / search / fetch / http）
    store/  SQLite + 仓储 + 密钥
    utils/  纯工具（禁 import 业务包）
    tools/  门禁脚本
    tests/  自动化测试

P0 阶段只落位骨架与 agent 内核移植，业务模块在 P1~P5 逐步补齐。
"""

from __future__ import annotations

__version__ = "0.0.1-p0"

__all__ = ["__version__"]
