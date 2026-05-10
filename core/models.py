from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict

@dataclass
class VariableConfig:
    """
    核心数据模型：描述一个变量的配置及其依赖关系。
    """
    name: str
    var_type: str  # e.g., 'integer', 'float', 'string', 'array'
    source_type: str # e.g., 'range', 'list', 'code', 'api'
    params: Dict[str, Any] = field(default_factory=dict) # 存储具体参数，如 {'min': 1, 'max': 100} 或 {'code': '...'}
    separator: str = '\n'
    loop_count: int = 1 # 循环生成次数
    dependencies: List[str] = field(default_factory=list) # 依赖的变量名列表，用于构建依赖图
