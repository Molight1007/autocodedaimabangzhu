"""变量类型推断器。

职责：
- 扫描 Python 源代码，通过 AST 分析变量赋值和类型注解来推断变量类型。
- 为代码补全提供 obj.attr 上下文中的 obj 类型信息。

不负责：
- 不执行代码，不保证类型推断完全准确（仅提供补全候选，不显著影响编辑器行为）。
- 不做控制流/数据流分析，不追踪跨函数/模块的类型传播。
- 不推断自定义类的属性。

约束：
- AST 解析失败时安全降级为空映射，不影响编辑器其他功能。
- 结果随每次 analyze() 调用全量重建，旧映射被覆盖。

数据流：
源代码 → ast.parse() → 遍历 Assign/AnnAssign/FunctionDef 节点 →
提取字面量类型 + 注解类型 + 构造调用 → {var_name: type_name}
"""

from __future__ import annotations

import ast
from typing import Dict, Optional

# ── 构造调用 → 类型名 ──────────────────────────────────────
_CONSTRUCTOR_TYPES: Dict[str, str] = {
    "str": "str",
    "int": "int",
    "float": "float",
    "list": "list",
    "dict": "dict",
    "set": "set",
    "tuple": "tuple",
    "bool": "bool",
    "range": "range",
    "frozenset": "frozenset",
    "bytes": "bytes",
    "bytearray": "bytearray",
    "enumerate": "enumerate",
    "zip": "zip",
    "map": "map",
    "filter": "filter",
    "reversed": "reversed",
}

# 注解中可识别为内置类型的名称
_ANNOTATION_TYPES: set[str] = {
    "str", "int", "float", "list", "dict", "set", "tuple", "bool",
    "bytes", "bytearray", "range", "frozenset",
}


def _infer_annotation(node: ast.AST) -> Optional[str]:
    """从类型注解 AST 节点提取类型名。"""
    if isinstance(node, ast.Name):
        name = node.id
        return name if name in _ANNOTATION_TYPES else None
    # List[int] / Dict[str, int] 等泛型提取基础类型
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name):
            name = node.value.id
            return name if name in _ANNOTATION_TYPES else None
    return None


def _infer_value(node: ast.AST) -> Optional[str]:
    """从赋值右侧 AST 节点推断类型。"""
    # 字面量
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, str):
            return "str"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        if v is None:
            return "NoneType"
        if isinstance(v, bytes):
            return "bytes"
        if isinstance(v, complex):
            return "complex"

    # 容器字面量
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Set):
        return "set"
    if isinstance(node, ast.Tuple):
        return "tuple"

    # 构造调用: str(x), int(x), list(iter), range(n) ...
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return _CONSTRUCTOR_TYPES.get(node.func.id)

    return None


class TypeInferrer:
    """从 Python 源代码推断变量 → 内置类型 的映射。

    典型用法:
        inferrer = TypeInferrer()
        types = inferrer.analyze(source_code)
        tp = inferrer.get_type("my_var")
    """

    def __init__(self):
        self._var_types: Dict[str, str] = {}

    def analyze(self, source: str) -> Dict[str, str]:
        """扫描源代码，重建 {变量名: 类型名} 映射。

        Args:
            source: 完整的 Python 源代码文本。

        Returns:
            变量名到类型名的字典。若 AST 解析失败则返回空 dict。
        """
        self._var_types.clear()
        stripped = source.strip()
        if not stripped:
            return {}

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {}

        for node in ast.walk(tree):
            # 带注解的赋值: s: str = "hello" 或 data: List[int]
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                type_name = _infer_annotation(node.annotation)
                if type_name:
                    self._var_types[node.target.id] = type_name
                elif node.value:
                    inferred = _infer_value(node.value)
                    if inferred:
                        self._var_types[node.target.id] = inferred

            # 普通赋值: s = "hello", s = str(x), a = []
            elif isinstance(node, ast.Assign):
                inferred = _infer_value(node.value)
                if inferred:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self._var_types[target.id] = inferred

            # 函数参数注解: def solve(data: str, n: int):
            if isinstance(node, ast.FunctionDef):
                for arg in node.args.args:
                    if arg.annotation:
                        type_name = _infer_annotation(arg.annotation)
                        if type_name:
                            self._var_types[arg.arg] = type_name

        return dict(self._var_types)

    def get_type(self, var_name: str) -> Optional[str]:
        """查询变量名的推断类型，未找到返回 None。"""
        return self._var_types.get(var_name)
