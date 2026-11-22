# -*- coding: utf-8 -*-
"""兼容层。1.3.0 起取消在线授权，这个模块不再有网络动作。

1.2.x 它负责：激活（拿码换 token）、心跳（确认没被吊销）、失效标记、
离线兜底。现在全是历史 —— 没有授权就没有可查的状态。

只保留三个还有用的函数：

  · machine()      机器码（装机统计用）
  · base_url()     服务端地址（版本检查也要用同一个域名）
  · reset_all()    清掉旧版留下的 license.json / token.json / 标记文件

其余一律空实现。函数保留的原因是 app.py 有调用点，删签名就要改 app.py，
改得越多回归面越大。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import license as lic

#: 默认服务端。换服务器只改这一处；也允许 config.json 覆盖。
DEFAULT_URL = "http://127.0.0.1:8080/api/license"

TOKEN_FILE = "token.json"
CFG_FILE = "config.json"
MARK_FILE = "license.mark.json"


# ---------------------------------------------------------------- 配置

def _cfg(data_dir: Path) -> dict:
    try:
        d = json.loads((Path(data_dir) / CFG_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def base_url(data_dir: Path) -> str:
    u = str(_cfg(data_dir).get("license_url") or DEFAULT_URL).strip()
    return u.rstrip("/")


# ---------------------------------------------------------------- 机器码

def machine() -> str:
    return lic.machine_code()


# ---------------------------------------------------------------- 清理

def _unlink(p: Path) -> bool:
    try:
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False


def reset_all(data_dir: Path) -> None:
    """清掉旧版残留。用户点「清空本机授权」时走这条 —— 按钮还在，
    但现在只是把历史文件扫掉，不影响能不能用。"""
    d = Path(data_dir)
    for name in (TOKEN_FILE, MARK_FILE, "ticket.json"):
        _unlink(d / name)
    lic.clear_license(d)


# ---------------------------------------------------------------- 空实现
#
# 下面这些是 app.py / ticket.py 的旧调用点。签名不变，行为是「一直可用」。

def load_token(data_dir: Path) -> dict:
    return {}


def save_token(data_dir: Path, rec: dict) -> bool:
    return True


def clear_token(data_dir: Path) -> bool:
    return True


def activate(data_dir: Path, code: str) -> dict:
    """旧接口。不再需要激活。"""
    return {"ok": True, "offline": False, "machine": machine(),
            "expire": "永久", "note": "",
            "msg": "1.3.0 起无需激活，打开就能用"}


def heartbeat(data_dir: Path, force: bool = False) -> dict:
    return {"ok": True, "kind": "ok", "expire": "永久", "note": "",
            "msg": "无需授权"}


def state(data_dir: Path, force: bool = False) -> dict:
    return {"kind": "ok", "machine": machine(), "expire": "永久",
            "note": "", "msg": "无需授权", "server": base_url(data_dir),
            "offline": False}


def accepted(data_dir: Path, force: bool = False) -> bool:
    return True


def clear_fail_throttle(data_dir: Path) -> None:
    return None


#: ticket.py 里 `licnet._post` 的旧引用。保留一个空壳，避免有人 import 到它
#: 时炸出 AttributeError。
def _post(url: str, body: dict) -> tuple[Optional[dict], str]:
    return None, "1.3.0 起不再有在线授权接口"


def _read_mark(data_dir: Path) -> dict:
    return {}


def _write_mark(data_dir: Path, kind: str, msg: str) -> None:
    return None
