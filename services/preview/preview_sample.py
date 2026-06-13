from __future__ import annotations

import ast
import importlib
import itertools
import math
import random
import statistics
import string
from typing import Any, Callable, Dict, Hashable, List, Optional, TypeVar

from core.models import VariableConfig
from services.preview.range_distribution import generate_range_value as generate_range_distribution_value
from services.preview.preview_policy import PreviewPolicy, truncate_cell
from utils.text_syntax import expand_charset_spec, parse_escaped_csv


T = TypeVar("T")


def build_preview_samples(
        configs: List[VariableConfig],
        rows: int,
        policy: Optional[PreviewPolicy] = None,
        *,
        no_duplicates: bool = False,
) -> List[Dict[str, Any]]:
    policy = policy or PreviewPolicy()
    rows = max(1, int(rows))
    columns = _unique_column_names([cfg.name or "" for cfg in configs])

    def _build_one_record() -> Dict[str, Any]:
        vars_dict: Dict[str, Any] = {}
        env: Dict[str, Any] = {"vars": vars_dict}
        record: Dict[str, Any] = {}
        for idx, cfg in enumerate(configs):
            v = _preview_cell(cfg, policy, env)
            record[columns[idx]] = v
            name = (cfg.name or "").strip()
            if name:
                vars_dict[name] = v
                if name.isidentifier():
                    env[name] = v
        return record
    return _build_samples_with_optional_dedup(
        rows=rows,
        builder=_build_one_record,
        no_duplicates=no_duplicates,
        key_builder=lambda record: tuple(str(record.get(col, "")) for col in columns),
    )


def build_generated_samples(
    configs: List[VariableConfig],
    rows: int,
    *,
    no_duplicates: bool = False,
) -> List[Dict[str, Any]]:
    rows = max(1, int(rows or 1))
    policy = PreviewPolicy(
        max_cells=max(5000, rows * max(1, len(configs))),
        hard_max_rows=max(100, rows),
        max_cell_chars=2_000_000,
        max_loop_items_per_cell=10_000,
    )
    return build_preview_samples(configs, rows, policy=policy, no_duplicates=bool(no_duplicates))


def build_problem_set(
    configs: List[VariableConfig],
    rows: int,
    *,
    no_duplicates: bool = False,
) -> Dict[str, Any]:
    rows = max(1, int(rows or 1))
    cases = _build_cases(configs, rows, no_duplicates=bool(no_duplicates))
    problem_set: Dict[str, Any] = {"count": len(cases), "cases": cases}
    title = _extract_problem_title(cases)
    if title:
        problem_set["title"] = title
    generate_data_code = _extract_generate_data_code(configs)
    if generate_data_code:
        problem_set["generate_data_code"] = generate_data_code
    return problem_set


def _extract_problem_title(cases: List[Dict[str, Any]]) -> str:
    for item in list(cases or []):
        case = dict(item or {})
        title = str(case.get("title") or "").strip()
        if title:
            return title
        meta = case.get("meta")
        if isinstance(meta, dict):
            title = str(meta.get("title") or "").strip()
            if title:
                return title
    return ""


def _extract_generate_data_code(configs: List[VariableConfig]) -> str:
    for cfg in list(configs or []):
        source = str(getattr(cfg, "source_type", "") or "").strip().lower()
        if source != "code":
            continue
        params = getattr(cfg, "params", None)
        if not isinstance(params, dict):
            continue
        code = str(params.get("code") or "").strip()
        if code:
            return code
    return ""


def _build_cases(configs: List[VariableConfig], rows: int, *, no_duplicates: bool) -> List[Dict[str, Any]]:
    configs = list(configs or [])
    rows = max(1, int(rows or 1))

    def _build_one_case() -> Dict[str, Any]:
        vars_dict: Dict[str, Any] = {}
        env: Dict[str, Any] = {"vars": vars_dict}
        case_data: Dict[str, Any] = {"stdout": "", "meta": {}}
        stdin_parts: List[str] = []

        for idx, cfg in enumerate(configs):
            cell_text, extra = _generate_cell_text_and_extra(cfg, env)
            stdin_parts.append(cell_text)

            sep = "" if cfg.separator is None else str(cfg.separator)
            if sep and idx < len(configs) - 1:
                stdin_parts.append(sep)

            name = (cfg.name or "").strip()
            if name:
                vars_dict[name] = cell_text
                if name.isidentifier():
                    env[name] = _coerce_env_value(cell_text, cfg.var_type)

            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k == "stdin":
                        continue
                    if k == "meta" and isinstance(v, dict):
                        meta = case_data.get("meta")
                        if isinstance(meta, dict):
                            meta.update(v)
                        else:
                            case_data["meta"] = dict(v)
                        continue
                    if k == "stdout":
                        s = "" if v is None else str(v)
                        if s.strip():
                            case_data["stdout"] = s
                        continue
                    case_data[k] = v

        stdin_text = "".join(stdin_parts)
        if not stdin_text.endswith("\n"):
            stdin_text += "\n"

        case_data["stdin"] = stdin_text
        if "stdout" not in case_data:
            case_data["stdout"] = ""
        if "meta" not in case_data or not isinstance(case_data.get("meta"), dict):
            case_data["meta"] = {}
        case_data["vars"] = vars_dict
        return case_data
    return _build_samples_with_optional_dedup(
        rows=rows,
        builder=_build_one_case,
        no_duplicates=no_duplicates,
        key_builder=lambda case: str(case.get("stdin") or ""),
    )


def _generate_cell_text_and_extra(cfg: VariableConfig, env: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
    loop_count = _resolve_loop_count(cfg.loop_count, env)

    sep = "" if cfg.separator is None else str(cfg.separator)
    parts: List[str] = []
    last_extra: Optional[Dict[str, Any]] = None

    for _ in range(loop_count):
        v = _generate_value(cfg, env)
        if isinstance(v, dict):
            last_extra = dict(v)
            parts.append(str(v.get("stdin") or ""))
        else:
            parts.append("" if v is None else str(v))

    return sep.join(parts), last_extra


def _build_samples_with_optional_dedup(
    rows: int,
    builder: Callable[[], T],
    *,
    no_duplicates: bool,
    key_builder: Callable[[T], Hashable],
) -> List[T]:
    rows = max(1, int(rows or 1))
    if not no_duplicates:
        return [builder() for _ in range(rows)]

    out: List[T] = []
    seen: set[Hashable] = set()
    attempts = 0
    max_attempts = max(rows, min(rows * 500, 50000))

    while len(out) < rows and attempts < max_attempts:
        attempts += 1
        item = builder()
        key = key_builder(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    while len(out) < rows:
        out.append(builder())

    return out


def _generate_value(cfg: VariableConfig, env: Dict[str, Any]) -> Any:
    return _generate_value_for_mode(cfg, env, mode="case")


def _build_code_fallback_data(error: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    err_text = str(error or "unknown_generate_data_error").strip() or "unknown_generate_data_error"
    out_meta: Dict[str, Any] = {}
    if isinstance(meta, dict):
        out_meta.update(meta)
    out_meta["generate_data_error"] = err_text
    # Keep stdin non-empty so generation failures are explicit.
    return {
        "stdin": "__GENERATE_DATA_ERROR__\n",
        "stdout": "",
        "meta": out_meta,
    }


def _preview_cell(cfg: VariableConfig, policy: PreviewPolicy, env: Dict[str, Any]) -> Any:
    loop_count = _resolve_loop_count(cfg.loop_count, env)

    max_items = max(1, int(policy.max_loop_items_per_cell))
    emit_count = min(loop_count, max_items)

    sep = "" if cfg.separator is None else str(cfg.separator)
    raw_values = [_preview_value(cfg, env) for _ in range(emit_count)]

    if loop_count == 1 and emit_count == 1:
        single = raw_values[0]
        if single is None:
            return ""
        if isinstance(single, (int, float, bool)):
            rendered = _normalize_cell_text(str(single))
            truncated = truncate_cell(rendered, policy)
            return truncated if truncated != rendered else single
        rendered = _normalize_cell_text("" if single is None else str(single))
        return truncate_cell(rendered, policy)

    s = sep.join("" if v is None else str(v) for v in raw_values)

    if loop_count > emit_count:
        s = s + (sep if sep else "") + "…"

    s = _normalize_cell_text(s)
    return truncate_cell(s, policy)


def _preview_value(cfg: VariableConfig, env: Dict[str, Any]) -> Any:
    return _generate_value_for_mode(cfg, env, mode="preview")


def _generate_value_for_mode(cfg: VariableConfig, env: Dict[str, Any], *, mode: str) -> Any:
    t = (cfg.var_type or "").lower()
    source = (cfg.source_type or "").lower()
    params = cfg.params or {}

    if source == "range":
        return _generate_range_value(var_type=t, params=params, env=env)

    if source == "list":
        return _generate_list_value(params)

    if source == "charset":
        return _generate_charset_value(params, env)

    if source == "code":
        if mode == "preview":
            return _generate_preview_code_value(params, env)
        return _generate_case_code_value(params, env)

    return _generate_default_value(t)


def _generate_range_value(var_type: str, params: Dict[str, Any], env: Dict[str, Any]) -> Any:
    vmin = _resolve_number_expr(params.get("min"), default=0, env=env)
    vmax = _resolve_number_expr(params.get("max"), default=vmin + 100, env=env)
    return generate_range_distribution_value(
        var_type=var_type,
        vmin=vmin,
        vmax=vmax,
        params=params,
    )


def _generate_list_value(params: Dict[str, Any]) -> str:
    raw = str(params.get("items") or "")
    items = parse_escaped_csv(raw)
    if not items:
        return ""
    return random.choice(items)


def _generate_charset_value(params: Dict[str, Any], env: Dict[str, Any]) -> str:
    min_len, max_len = _resolve_range_or_single_expr(params.get("length"), default_min=10, default_max=10, env=env)
    length = random.randint(int(min_len), int(max_len))
    charset = str(params.get("charset") or "a-zA-Z0-9").strip()
    alphabet = expand_charset_spec(charset)
    if not alphabet:
        alphabet = expand_charset_spec("a-zA-Z0-9")
    return "".join(random.choice(alphabet) for _ in range(max(0, length)))


def _generate_case_code_value(params: Dict[str, Any], env: Dict[str, Any]) -> Any:
    code = str(params.get("code") or "")
    expr = _pick_expression_from_code(code)
    expr_error: Optional[Exception] = None
    try:
        if expr:
            return _safe_eval_expr(expr, env)
    except Exception as e:
        expr_error = e
    try:
        result = _safe_exec_generate_data_dict(code, env)
        if result is not None:
            stdin_text = str(result.get("stdin") or "")
            if stdin_text.strip():
                return result
            return _build_code_fallback_data("empty_stdin_from_generate_data", result.get("meta"))
    except Exception as e:
        return _build_code_fallback_data(e)
    if expr_error is not None:
        return _build_code_fallback_data(expr_error)
    return _build_code_fallback_data("empty_code_result")


def _generate_preview_code_value(params: Dict[str, Any], env: Dict[str, Any]) -> Any:
    code = str(params.get("code") or "")
    expr = _pick_expression_from_code(code)
    try:
        if expr:
            return _safe_eval_expr(expr, env)
    except Exception:
        pass
    try:
        stdin = _safe_exec_generate_data(code, env)
        if stdin is not None:
            return stdin
    except Exception:
        pass
    return ""


def _generate_default_value(var_type: str) -> Any:
    if var_type == "integer":
        return random.randint(0, 100)
    if var_type == "float":
        return random.random()
    if var_type == "boolean":
        return random.choice([True, False])
    return ""


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _coerce_env_value(value: Any, var_type: str) -> Any:
    t = str(var_type or "").strip().lower()
    if t == "integer":
        try:
            return int(_to_float(value, default=0))
        except Exception:
            return value
    if t == "float":
        try:
            return float(_to_float(value, default=0.0))
        except Exception:
            return value
    if t == "boolean":
        s = str(value or "").strip().lower()
        if s in {"true", "1", "yes", "y"}:
            return True
        if s in {"false", "0", "no", "n"}:
            return False
    return value


def _resolve_number_expr(value: Any, default: float, env: Dict[str, Any]) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return float(default)
    s = str(value).strip()
    if not s:
        return float(default)
    try:
        return float(s)
    except Exception:
        pass
    try:
        resolved = _safe_eval_expr(s, env)
        return float(resolved)
    except Exception:
        return float(default)


def _resolve_int_expr(value: Any, default: int, env: Dict[str, Any], min_value: Optional[int] = None) -> int:
    out = int(_resolve_number_expr(value, default=default, env=env))
    if min_value is not None:
        out = max(int(min_value), out)
    return out


def _resolve_range_or_single_expr(value: Any, default_min: float, default_max: float, env: Dict[str, Any]) -> tuple[float, float]:
    if value is None:
        return float(default_min), float(default_max)
    s = str(value).strip()
    if not s:
        return float(default_min), float(default_max)

    if "," in s:
        left, right = s.split(",", 1)
        lo = _resolve_number_expr(left, default=default_min, env=env)
        hi = _resolve_number_expr(right, default=default_max, env=env)
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi

    try:
        resolved = _safe_eval_expr(s, env)
        if isinstance(resolved, (tuple, list)) and len(resolved) >= 2:
            lo = _resolve_number_expr(resolved[0], default=default_min, env=env)
            hi = _resolve_number_expr(resolved[1], default=default_max, env=env)
            if hi < lo:
                lo, hi = hi, lo
            return lo, hi
        n = _resolve_number_expr(resolved, default=default_min, env=env)
        return n, n
    except Exception:
        n = _resolve_number_expr(s, default=default_min, env=env)
        return n, n


def _resolve_loop_count(value: Any, env: Dict[str, Any]) -> int:
    min_count, max_count = _resolve_range_or_single_expr(value, default_min=1, default_max=1, env=env)
    lo = max(1, int(min_count))
    hi = max(1, int(max_count))
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def _unique_column_names(names: List[str]) -> List[str]:
    used: Dict[str, int] = {}
    out: List[str] = []
    for i, raw in enumerate(names):
        base = raw.strip() or f"var_{i + 1}"
        n = used.get(base, 0) + 1
        used[base] = n
        out.append(base if n == 1 else f"{base} ({n})")
    return out


def _first_non_empty_line(text: str) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s:
            return s
    return ""


def _normalize_cell_text(text: str) -> str:
    s = "" if text is None else str(text)
    return s.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def _pick_expression_from_code(code: str) -> str:
    src = str(code or "")
    if "def generate_data" in src:
        return ""
    picked = ""
    for ln in src.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if s.startswith(("def ", "class ", "import ", "from ", "return ", "@")):
            continue
        if s in {"{", "}", "[", "]", "(", ")", ","}:
            continue
        picked = s
    return picked


def _safe_eval_expr(expr: str, env: Dict[str, Any]) -> Any:
    tree = ast.parse(expr, mode="eval")
    _validate_safe_expr_ast(tree)
    safe_globals = {
        "__builtins__": _safe_builtins(),
        "itertools": itertools,
        "math": math,
        "random": random,
        "statistics": statistics,
        "string": string,
    }
    return eval(compile(tree, "<preview_expr>", "eval"), safe_globals, env or {})


def _safe_builtins() -> Dict[str, Any]:
    return {
        "None": None,
        "True": True,
        "False": False,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
    }


def _safe_exec_generate_data(code: str, env: Dict[str, Any]) -> Optional[str]:
    tree = ast.parse(code or "", mode="exec")
    _validate_safe_exec_ast(tree)

    allowed_modules = {"random", "math", "itertools", "string", "statistics"}

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = (name or "").split(".", 1)[0]
        if root not in allowed_modules:
            raise ImportError("module_not_allowed")
        return importlib.import_module(name)

    safe_globals = {
        "__builtins__": {**_safe_builtins(), "__import__": safe_import},
        "itertools": itertools,
        "math": math,
        "random": random,
        "statistics": statistics,
        "string": string,
    }

    namespace = dict(safe_globals)
    if isinstance(env, dict):
        namespace.update(env)
    exec(compile(tree, "<preview_code>", "exec"), namespace, namespace)
    if isinstance(env, dict):
        env.update(namespace)
    fn = namespace.get("generate_data")
    if not callable(fn):
        return None

    result = fn({})
    if isinstance(result, dict):
        stdin = result.get("stdin")
        if stdin is None:
            return ""
        return str(stdin)
    if result is None:
        return ""
    return str(result)


def _safe_exec_generate_data_dict(code: str, env: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tree = ast.parse(code or "", mode="exec")
    _validate_safe_exec_ast(tree)

    allowed_modules = {"random", "math", "itertools", "string", "statistics"}

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = (name or "").split(".", 1)[0]
        if root not in allowed_modules:
            raise ImportError("module_not_allowed")
        return importlib.import_module(name)

    safe_globals = {
        "__builtins__": {**_safe_builtins(), "__import__": safe_import},
        "itertools": itertools,
        "math": math,
        "random": random,
        "statistics": statistics,
        "string": string,
    }

    namespace = dict(safe_globals)
    if isinstance(env, dict):
        namespace.update(env)
    exec(compile(tree, "<generate_data>", "exec"), namespace, namespace)
    if isinstance(env, dict):
        env.update(namespace)
    fn = namespace.get("generate_data")
    if not callable(fn):
        return None

    result = fn({})
    if isinstance(result, dict):
        out = dict(result)
        if "stdin" not in out:
            out["stdin"] = ""
        if "stdout" not in out:
            out["stdout"] = ""
        if "meta" not in out or not isinstance(out.get("meta"), dict):
            out["meta"] = {}
        return out
    if result is None:
        return {"stdin": "", "stdout": "", "meta": {}}
    return {"stdin": str(result), "stdout": "", "meta": {}}


def _validate_safe_expr_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _safe_builtins():
                    raise ValueError("unsafe_call")
            elif isinstance(node.func, ast.Attribute):
                if not isinstance(node.func.value, ast.Name):
                    raise ValueError("unsafe_attr_call")
                if node.func.value.id not in {"random", "math", "itertools", "string", "statistics"}:
                    raise ValueError("unsafe_attr_call")
                if node.func.attr.startswith("_"):
                    raise ValueError("unsafe_attr_call")
            else:
                raise ValueError("unsafe_call")
            continue

        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                raise ValueError("unsafe_attribute")
            if node.value.id not in {"random", "math", "itertools", "string", "statistics"}:
                raise ValueError("unsafe_attribute")
            if node.attr.startswith("_"):
                raise ValueError("unsafe_attribute")
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Lambda, ast.FunctionDef, ast.ClassDef)):
            raise ValueError("unsafe_node")

        if isinstance(node, (ast.Global, ast.Nonlocal, ast.Await, ast.Yield, ast.YieldFrom)):
            raise ValueError("unsafe_node")

        if isinstance(node, (ast.Try, ast.Raise, ast.While, ast.For, ast.With, ast.AsyncWith, ast.AsyncFor)):
            raise ValueError("unsafe_node")

        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("unsafe_name")


def _validate_safe_exec_ast(tree: ast.AST) -> None:
    allowed_modules = {"random", "math", "itertools", "string", "statistics"}
    blocked_call_names = {"exec", "eval", "compile", "open", "__import__", "input"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".", 1)[0]
                if root not in allowed_modules:
                    raise ValueError("import_not_allowed")
            continue

        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".", 1)[0] if mod else ""
            if root and root not in allowed_modules:
                raise ValueError("import_not_allowed")
            continue

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in blocked_call_names:
                raise ValueError("blocked_call")
            continue

        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("unsafe_attribute")

        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError("unsafe_name")
