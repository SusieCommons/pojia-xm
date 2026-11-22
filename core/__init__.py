"""目标注册表。

加新客户端只需要两步：写一个继承 Target 的类，再挂进 REGISTRY。
`all_targets()` 的顺序就是界面上的显示顺序。
"""
from __future__ import annotations

from .base import (BACKUP_ROOT, DATA_DIR, Hit, Report, Target, find_target_backup,
                   latest_backup, load_state, new_backup, save_state)
from .codex import Codex
from .dsh import DSH
from .wb import WorkBuddy
from .zcode import ZCode

REGISTRY: dict[str, type[Target]] = {
    "codex": Codex,
    "wb": WorkBuddy,
    "zcode": ZCode,
    "dsh": DSH,
}

#: 显示名，界面用
LABELS = {
    "codex": "Codex",
    "wb": "WorkBuddy",
    "zcode": "ZCode",
    "dsh": "DSH",
}

ORDER = ["codex", "wb", "zcode", "dsh"]


def get(name: str) -> Target:
    cls = REGISTRY[name]
    return cls()


def all_targets() -> list[Target]:
    return [REGISTRY[n]() for n in ORDER]


def resolve(names: list[str] | None) -> list[Target]:
    if not names or names == ["all"]:
        return all_targets()
    out = []
    for n in names:
        n = n.strip().lower()
        if n in REGISTRY:
            out.append(get(n))
    return out


__all__ = [
    "REGISTRY", "LABELS", "ORDER", "get", "all_targets", "resolve",
    "Target", "Hit", "Report", "DATA_DIR", "BACKUP_ROOT",
    "latest_backup", "find_target_backup", "load_state", "save_state", "new_backup",
]
