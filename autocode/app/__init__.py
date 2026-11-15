"""autoCode 编排层（P4 补齐）。

规划模块：
- problem_service.py  题库读写与检索
- prep_service.py     预生成流水线 + Budget 熔断（R8，顶层持有）
- study_service.py    学生做题与诊断闭环
- runner_bridge.py    QProcess 运行桥（不进 engine，engine 保持纯函数）
- prep_job.py         预生成后台任务（QThread 五信号）

约束：本包是 ui/ 唯一允许依赖的业务包（ui 只能经 app）。
"""

from __future__ import annotations

__all__: list[str] = []
