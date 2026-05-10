from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PreviewPolicy:
    max_cells: int = 5000
    hard_max_rows: int = 100
    hard_min_rows: int = 1
    max_cell_chars: int = 200
    max_loop_items_per_cell: int = 20


def choose_preview_rows(
    total_rows: int,
    field_count: int,
    policy: Optional[PreviewPolicy] = None,
) -> int:
    policy = policy or PreviewPolicy()

    total_rows = max(0, int(total_rows))
    field_count = max(0, int(field_count))

    if total_rows <= 0:
        total_rows = 1

    if field_count <= 0:
        return max(policy.hard_min_rows, min(total_rows, policy.hard_max_rows))

    by_cells = max(policy.hard_min_rows, policy.max_cells // max(1, field_count))
    return max(
        policy.hard_min_rows,
        min(total_rows, policy.hard_max_rows, by_cells),
    )


def truncate_cell(text: str, policy: Optional[PreviewPolicy] = None) -> str:
    policy = policy or PreviewPolicy()
    s = "" if text is None else str(text)
    if len(s) <= policy.max_cell_chars:
        return s
    return s[: policy.max_cell_chars - 1] + "…"

