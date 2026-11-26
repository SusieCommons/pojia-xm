# -*- coding: utf-8 -*-
"""兼容层。1.3.0 起取消短期票据，这个模块不再联网。

1.2.x 它负责「此刻能不能干活」：每 10 分钟向服务端要一张 Ed25519 签名的
短期票，本地只验签和看 exp。目的是让破解收益从「破一次永久用」变成
「破一次十分钟后还要再联网」。

1.3.0 取消授权之后这层没有存在意义了 —— 没有人再被拦。保留这个文件是
因为 app.py 有 `ticket.required()` / `ticket.fresh()` 的调用点，删签名
就要动 app.py，改得越多回归面越大。

现在所有函数都返回「通过」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

TICKET_FILE = "ticket.json"

#: 版本号。跟打包时的文件版本保持一致。
#:
#: 1.3.0 起票据体系已拆掉（本模块所有函数都是空壳），这个常量只剩
#: 「有没有人还在读」的兼容意义。改版本号时一起改，别留着旧值 ——
#: 一个写着 1.3.0 的常量出现在 1.3.2 的包里，排查的人会以为版本没升。
VER = "1.4.1"


# ---------------------------------------------------------------- 存取

def load(data_dir: Path) -> dict:
    return {}


def _write(data_dir: Path, rec: dict) -> None:
    return None


def clear(data_dir: Path) -> None:
    try:
        (Path(data_dir) / TICKET_FILE).unlink()
    except OSError:
        pass


def forget_failure(data_dir: Path) -> None:
    return None


# ---------------------------------------------------------------- 开关

def required(data_dir: Path) -> bool:
    """永远不要求票据。"""
    return False


def _disable(data_dir: Path, why: str) -> None:
    return None


def _disable_expired(data_dir: Path) -> bool:
    return True


# ---------------------------------------------------------------- 验签

def verify(ticket: str) -> tuple[Optional[dict], str]:
    """旧接口。没有票据可验。"""
    return None, "1.3.0 起不再使用票据"


# ---------------------------------------------------------------- 申请

def fetch(data_dir: Path, token: str) -> tuple[Optional[str], str, bool, bool]:
    return None, "1.3.0 起不再使用票据", True, False


def get(data_dir: Path, force: bool = False) -> dict:
    return {"ok": True, "payload": None, "msg": "无需票据", "exp": 0}


def fresh(data_dir: Path, force: bool = False) -> dict:
    return {"ok": True, "payload": None, "msg": "无需票据", "exp": 0}


def remain(data_dir: Path) -> int:
    return 0


# ---------------------------------------------------------------- 旧接口

def build_hash() -> str:
    """1.2.x 用来算自身 exe 的 SHA-256 报给服务端验白名单。

    1.3.0 不再有白名单，但这个值仍被旧发布脚本读取 —— 保留一个实现，
    让那些脚本不至于崩。冻结态算 exe，源码态算 app.py。
    """
    import hashlib
    import sys
    from pathlib import Path as _P
    try:
        if getattr(sys, "frozen", False):
            p = _P(sys.executable)
        else:
            p = _P(__file__).resolve().parent.parent / "app.py"
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""
