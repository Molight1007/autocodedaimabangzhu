"""教学层 API 适配：探针与五步提示的路由入口。

从 tutor.py 拆出来的理由与 `api_feedback.py` 相同：server.py 只做参数搬运，
业务判定都在 tutor；本文件负责把 payload 转成 tutor 要的形状、把
TutorError 转成 `{error, code}` 返回约定。生成类操作（探针包 / 提示包 / L4）
需要调用者本人的模型凭据，`runner` 由 server 按登录人构造后传入——
绝不复用别人的额度。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import tutor
from .llm_stage import StageRunner


def _tutor_call(func, *args, **kwargs) -> Dict[str, Any]:
    """统一兜底：TutorError → {error, code}；KeyError（题不存在）照常上抛给 server。"""
    try:
        return func(*args, **kwargs)
    except tutor.TutorError as exc:
        return {"error": str(exc), "code": exc.code}


def start_probe(
    db: Any, runner: StageRunner, problem_id: int, user: str
) -> Dict[str, Any]:
    return _tutor_call(tutor.start_probe, db, runner, int(problem_id), str(user))


def probe_status(db: Any, problem_id: int, user: str) -> Dict[str, Any]:
    return _tutor_call(tutor.probe_status, db, int(problem_id), str(user))


def probe_answer(
    db: Any, problem_id: int, user: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    return _tutor_call(tutor.probe_answer, db, int(problem_id), str(user), payload)


def start_ladder(
    db: Any, runner: StageRunner, problem_id: int, user: str
) -> Dict[str, Any]:
    return _tutor_call(tutor.start_ladder, db, runner, int(problem_id), str(user))


def ladder_status(db: Any, problem_id: int, user: str) -> Dict[str, Any]:
    return _tutor_call(tutor.ladder_status, db, int(problem_id), str(user))


def ladder_present(
    db: Any, problem_id: int, user: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    return _tutor_call(
        tutor.ladder_present, db, int(problem_id), str(user),
        str(payload.get("trigger") or "self"),
    )


def ladder_answer(
    db: Any, problem_id: int, user: str, payload: Dict[str, Any],
    runner: Optional[StageRunner] = None,
) -> Dict[str, Any]:
    return _tutor_call(tutor.ladder_answer, db, int(problem_id), str(user), payload, runner)


def give_l4(
    db: Any, runner: StageRunner, problem_id: int, user: str
) -> Dict[str, Any]:
    return _tutor_call(tutor.ladder_give_l4, db, runner, int(problem_id), str(user))


def reset_tutor(db: Any, problem_id: int, user: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    full = bool(payload.get("full"))
    return _tutor_call(tutor.reset_tutor, db, int(problem_id), str(user), full)
