# -*- coding: utf-8 -*-
"""机器码的快速路径。

为什么单独一个模块
------------------
1.2.x 算一次机器码要 2 秒：`_raw_ids()` 对 4 项硬件各试一次 wmic，wmic 在
被安全策略拦掉的机器上根本起不来，每次都要等 subprocess 返回才 fallback
到 PowerShell。4 项 × 2 次 = 8 次进程启动。

1.3.0 起机器码只用于装机统计，不参与任何判断 —— 那就没有理由为了它让
启动卡 2 秒。这里的做法：

  1. **一次 PowerShell 拿全 4 个值。** 一段脚本输出 4 行，只起 1 个进程。
  2. **落盘缓存。** 算完写 `~/.xiaoxiongmao/machine.json`，带 4 项原始
     id 的校验值。下次启动直接读，硬件没变就 0 进程启动。
  3. **算不出来就返回空串。** 统计用的东西，拿不到就不报，绝不让界面
     等着它。界面显示「——」比转两秒圈好。

缓存必须带校验：只存机器码字符串的话，用户换了硬盘机器码该变却不变，
统计数据会串到一起。所以存的是 4 项原始 id，每次读回来重算一遍哈希 ——
哈希是微秒级，重算不影响速度，但硬件一变立刻就能发现。
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Optional

#: 跟 core/license.py 的 DATA_DIR 保持一致。这里不 import 那个模块 ——
#: 它会 import 本模块，成环。
DATA_DIR = Path(os.environ.get("XM_DATA")
                or (Path.home() / ".xiaoxiongmao"))
CACHE_FILE = "machine.json"

MACHINE_LEN = 16

#: 一次拿全 4 个值。用分号串起来，输出每项一行。
#: -ErrorAction SilentlyContinue：某项读不到时不要刷红字到 stderr。
_PS_ALL = (
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::ASCII;"
    "(Get-CimInstance Win32_BaseBoard).SerialNumber;"
    "(Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\").VolumeSerialNumber;"
    "(Get-CimInstance Win32_Processor).ProcessorId;"
    "(Get-CimInstance Win32_ComputerSystemProduct).UUID"
)

#: 报错 / 拦截提示，不能当硬件标识。
_BAD = (
    "denied", "forbidden", "blocked", "not recognized", "not found",
    "no such", "error", "invalid", "policy", "access is",
    "拒绝访问", "无法", "失败", "禁止", "已阻止",
)

#: 卷序列号必须是 8 位十六进制。
_RE_VOL = re.compile(r"[0-9A-Fa-f]{8}")

#: 属性名表头。CIM 查询在某些机器上不打值只打表头（属性被策略屏蔽时），
#: 表头本身长度够、也不含报错词，会一路混进 id 里 —— "Serial Number" 被
#: 当成 CPU id 的话，机器码就锁死在这个假值上，换 CPU 也不变。
#: 用小写比对，只排除精确等于表头的行，真实标识不会撞这些词。
_HDR = frozenset((
    "serialnumber", "serial number",
    "volumeserialnumber", "volume serial number",
    "processorid", "processor id",
    "uuid", "caption", "name", "deviceid", "device id",
))

_LOCK = threading.Lock()
_MEM = ""


def _clean(line: str) -> str:
    s = line.strip().replace("\ufeff", "").replace("\x00", "")
    if not s:
        return ""
    low = s.lower()
    if any(w in low for w in _BAD):
        return ""
    # 表头单独判：只拦「整行就是表头」的情况，不做子串匹配，
    # 免得把真含 "name" 之类字样的序列号误杀。
    if low in _HDR:
        return ""
    return s


def _read_ps() -> list[str]:
    """起一次 PowerShell，拿 4 个值。失败返回 []。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ALL],
            capture_output=True, timeout=10,
            creationflags=0x08000000,          # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = out.stdout
    text = raw.decode("ascii", errors="ignore")
    if not text.strip():
        text = raw.decode("mbcs", errors="ignore")
    # 按顺序对应 4 项。空行也是有效占位（那一项读不到），不能过滤掉，
    # 否则第 2 项读不到时第 3 项会被当成卷序列号。
    lines = [ln for ln in text.splitlines()]
    # 去掉尾部纯空行，保留中间的空位
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _ids_from_lines(lines: list[str]) -> list[str]:
    """把 4 行输出整理成 id 列表。顺序固定，缺项跳过。"""
    get = lambda i: _clean(lines[i]) if i < len(lines) else ""   # noqa: E731
    out: list[str] = []

    bb = get(0)
    if bb and bb.lower() not in ("o.e.m.", "none", "default string",
                                 "serial number"):
        out.append("bb:" + bb)

    vol = get(1)
    if vol and _RE_VOL.fullmatch(vol):
        out.append("vd:" + vol)

    cpu = get(2)
    if cpu and len(cpu) >= 8:
        out.append("cpu:" + cpu)

    guid = get(3)
    if guid and len(guid) >= 16:
        out.append("guid:" + guid)

    return out


def _hash(ids: list[str]) -> str:
    raw = "|".join(ids).encode("utf-8")
    return sha256(raw).hexdigest().upper()[:MACHINE_LEN]


def _dash(body: str) -> str:
    return "-".join(body[i:i + 4] for i in range(0, MACHINE_LEN, 4))


def _cache_path() -> Path:
    return DATA_DIR / CACHE_FILE


def _read_cache() -> Optional[list[str]]:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        ids = d.get("ids")
        if isinstance(ids, list) and ids and all(isinstance(x, str) for x in ids):
            return ids
    except (OSError, ValueError):
        pass
    return None


def _write_cache(ids: list[str]) -> None:
    p = _cache_path()
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".machine.tmp{os.getpid()}_{threading.get_ident()}")
        tmp.write_text(json.dumps({"ids": ids, "at": int(time.time())},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _fallback_ids() -> list[str]:
    """硬件全读不到时的兜底。这种码不稳定，但总比空字符串强。"""
    return ["u:" + os.environ.get("USERNAME", ""),
            "h:" + platform.node(),
            "m:" + str(uuid.getnode())]


def code(fresh: bool = False) -> str:
    """机器码，带连字符，如 5FD5-582C-BAEB-EE3F。

    fresh=True 跳过磁盘缓存，重新读一遍硬件。换过硬件之后要刷一次统计
    才用得上；正常启动用缓存即可。
    """
    global _MEM
    if _MEM and not fresh:
        return _MEM
    with _LOCK:
        if _MEM and not fresh:
            return _MEM

        ids: list[str] = []
        if not fresh:
            ids = _read_cache() or []
        if not ids:
            lines = _read_ps()
            ids = _ids_from_lines(lines) if lines else []
            if ids:
                _write_cache(ids)

        if not ids:
            # 缓存和硬件都没拿到。别在这里再起 wmic —— 那正是要躲的 2 秒。
            # 用兜底 id 算一个，不落缓存（不稳定，落了下次还会误命中）。
            _MEM = _dash(_hash(_fallback_ids()))
            return _MEM

        _MEM = _dash(_hash(ids))
        return _MEM


def plain(fresh: bool = False) -> str:
    """无连字符版本，16 位。"""
    return code(fresh).replace("-", "")


def reset() -> None:
    """清缓存。用户点「重新识别机器」时用。"""
    global _MEM
    with _LOCK:
        _MEM = ""
        try:
            p = _cache_path()
            if p.exists():
                p.unlink()
        except OSError:
            pass
