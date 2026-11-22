"""提示词包：`.md` 模板 + `{{var}}` 插值（见 loader.py）。"""

from __future__ import annotations

from .loader import (
    BRUTE,
    GENERATOR,
    HINTS,
    REF_SOLUTION,
    SLIM_ADVERSARIAL,
    SLIM_ADVERSARIAL_REVIEW,
    SLIM_DATASET,
    SLIM_DATASET_REVIEW,
    SLIM_REFERENCE,
    SLIM_SEGMENT,
    SPEC,
    TEMPLATE_NAMES,
    load,
    load_all,
    render,
    template_path,
)

__all__ = [
    "BRUTE",
    "GENERATOR",
    "HINTS",
    "REF_SOLUTION",
    "SLIM_ADVERSARIAL",
    "SLIM_ADVERSARIAL_REVIEW",
    "SLIM_DATASET",
    "SLIM_DATASET_REVIEW",
    "SLIM_REFERENCE",
    "SLIM_SEGMENT",
    "SPEC",
    "TEMPLATE_NAMES",
    "load",
    "load_all",
    "render",
    "template_path",
]
