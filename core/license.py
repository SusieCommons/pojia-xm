# -*- coding: utf-8 -*-
"""兼容层。1.3.0 起取消卡密授权，这个模块只剩「转发」作用。

变更记录
--------
1.2.x：Ed25519 离线验签 + 机器码绑定 + 到期日 + 在线 token + 短期票据，
四层闸门。用户要拿码、要激活、管理员要签发、要续期、要停用。

1.3.0：全部拆掉。任何人下载即用，无码、无机器码要求、无到期、无吊销。

为什么保留这个文件而不是删掉
----------------------------
core/licnet.py、core/ticket.py、app.py 都在 import 它。整片删掉要动
import 图、要重排启动顺序，漏一处就是 ImportError —— 用户拿到的是
「双击没反应」。所以留一层薄壳：签名不变，行为改成无条件通过。

机器码的真实计算在 core/fastid.py（一次 PowerShell + 磁盘缓存，~200ms）。
这里转发，保留旧名字给已有调用点。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import fastid as _fastid

LICENSE_FILE = "license.json"

#: 机器码长度。界面按 4 位一组显示。
MACHINE_LEN = 16


# ---------------------------------------------------------------- 机器码

def machine_code() -> str:
    """带连字符的机器码，如 5FD5-582C-BAEB-EE3F。仅用于装机统计。"""
    return _fastid.code()


def machine_code_plain() -> str:
    """无连字符版本，16 位。"""
    return _fastid.plain()


def reset_machine() -> None:
    """清掉机器码缓存，下次重新读硬件。"""
    _fastid.reset()


# ---------------------------------------------------------------- 兼容层

def state(data_dir: Path) -> dict:
    """永远「可用」。data_dir 保留只为不改调用点签名。"""
    return {"kind": "ok", "machine": machine_code(), "expire": "永久",
            "note": "", "msg": "无需授权"}


def accepted(data_dir: Path) -> bool:
    return True


def parse_license(code: str) -> tuple[Optional[dict], str]:
    """旧接口。现在只回报「不需要授权」。"""
    return None, "1.3.0 起已取消授权码，直接使用即可"


def check_machine(payload: dict) -> tuple[bool, str]:
    return True, ""


def check_expiry(payload: dict) -> tuple[bool, str]:
    return True, "永久"


def load_license(data_dir: Path) -> Optional[str]:
    """读旧授权码。只在「清历史残留」时用，不参与任何判断。"""
    try:
        d = json.loads((Path(data_dir) / LICENSE_FILE).read_text(encoding="utf-8"))
        return str(d.get("code") or "") or None
    except (OSError, ValueError):
        return None


def clear_license(data_dir: Path) -> bool:
    try:
        p = Path(data_dir) / LICENSE_FILE
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


def save_license(data_dir: Path, code: str, payload: dict) -> bool:
    """旧接口，空实现。不再往磁盘写授权码。"""
    return True
