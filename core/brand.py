# -*- coding: utf-8 -*-
"""引流信息。内置默认值写死在代码里 + 远端可下发（Ed25519 签名，改了必定验不过）。

为什么要有两层
--------------
1.3.0 起后台要能改引流内容（换群、改话术、活动期换个说法）。但「可配置」
和「可被逆向篡改」是两件相反的事：只要远端内容不验签，改 exe 里一行 URL
或者中间人劫持一次 HTTP 响应，就能把群号换成别人的 —— 用户按界面上的号
加进去，发现是别人的群，这个工具的署名就这么没了。

所以远端内容的形状是「**服务端私钥签名 + 客户端内置公钥验签**」：

    payload  = 规范化的 JSON（sort_keys / 紧凑分隔符 / UTF-8）
    signature= Ed25519(signer.key, payload)
    wire     = base64url(payload) + "." + base64url(signature)

客户端只认验签通过的 payload。三条硬约束：

  · **公钥内置在 exe 里**，不下载、不配置。想改公钥就得改 exe。
  · **改公钥 = 所有历史签名全部失效**。攻击者把公钥换成自己的，服务端
    签的东西一个都验不过，客户端退回内置默认值 —— 他拿到的不是「换成
    他的群」，而是「引流卡显示异常」。这比静默替换好得多。
  · **验不过就用内置默认值，绝不使用未验签的远端内容。** 网络挂了、服务
    端没配私钥、签名被中间人改了一个字节，全都走这条路。

内置默认值（DEFAULT_*）就是上一版写死的那份，删掉这层也还是能跑。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- 内置默认值
#
# 这几个常量是**兜底**，不是可选项。远端没配 / 验签失败 / 断网时界面就用这
# 一份，所以它不能是空的，也不能从任何外部来源读。

DEFAULT_GROUP_NO = "100000000"
DEFAULT_GROUP_NAME = "小熊猫交流群"
DEFAULT_TIP = "装完遇到问题、想要新版、想看别的工具，都进群说一声。"

#: 内置公钥（Ed25519，base64url 无填充的 32 字节）。
#: 跟服务端 server.py:PUBLIC_KEY_B64 是同一把，也跟 _keys/signer.key 成对。
#: **改私钥必须同时改这里并重新打包**，否则线上签名全废。
PUBLIC_KEY_B64 = "hCZudW9Cy9fu6RD_oA4oLwDpMwpK4ABeBTnjFgrLvOM"

#: 远端配置的缓存文件名与有效期。缓存也带签名，验不过就当没有。
CACHE_FILE = "brand.json"
CFG_FILE = "config.json"

#: 单个请求超时。跟版本检查同量级 —— 启动路径不能等。
TIMEOUT = 4.0

#: 同一进程内两次真实请求的最小间隔（秒）。
MIN_GAP = 120

_RE_GROUP_NO = re.compile(r"^\d{5,12}$")

#: 群名 / 话术里不允许出现的字符。这些是「排版用的」，不是内容：
#: 换行会把卡片撑破，制表符和零宽字符能用来藏第二段看不见的群号。
_BAD_CHARS = re.compile(r"[\r\n\t\u200b-\u200f\u2028-\u202e\u2060-\u206f]")

#: 字段长度上限。后台填超长文案不会让客户端布局崩，但会让人读到一坨垃圾，
#: 所以服务端和客户端都截断 —— 两边都截才是稳的，只截一边会被绕过。
MAX_NAME = 40
MAX_TIP = 200

# ---------------------------------------------------------------- 运行时状态

_LOCK = threading.Lock()
#: 当前生效的配置（已验签或内置默认）。
_ACTIVE: dict = {}
#: 上一次验签失败的原因，供界面显示。空 = 没问题。
#: 只在「远端确实下发了东西但验不过」时置位 —— 断网不算，那不是篡改。
_TAMPER = ""
_LAST_HIT = 0.0
_LAST_STATUS = "builtin"      # builtin | remote | cached | tamper | offline


# ---------------------------------------------------------------- 内置默认

def default() -> dict:
    """内置默认配置。永远可用，永远不需要网络。"""
    return {
        "group": DEFAULT_GROUP_NO,
        "name": DEFAULT_GROUP_NAME,
        "line": f"{DEFAULT_GROUP_NAME} {DEFAULT_GROUP_NO}",
        "url": _join_url(DEFAULT_GROUP_NO),
        "tip": DEFAULT_TIP,
        "src": "builtin",
        "at": "",
    }


def _join_url(group_no: str) -> str:
    return f"https://qm.qq.com/cgi-bin/qm/qr?k=&group_code={group_no}"


# ---------------------------------------------------------------- 规范化

def canon(payload: dict) -> bytes:
    """跟服务端 server.py:canon() 必须逐字节一致。

    sort_keys + 紧凑分隔符 + UTF-8。少一个参数签名就对不上，而且报错是
    「签名无效」这种笼统的话，排查起来很费时间 —— 改这里要同步改服务端。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _b64d(s: str) -> bytes:
    """base64url 解码，容忍无填充和标准 base64 的 +/ 变体。"""
    t = str(s or "").strip().replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


# ---------------------------------------------------------------- 验签

def verify_wire(wire: str) -> tuple[Optional[dict], str]:
    """验一条 `payload.sig`。返回 (payload, 错误说明)。

    只用标准库能做的事：Ed25519 必须有第三方实现，这里依赖 cryptography。
    它是 PyInstaller 的常规依赖（webview 链路本来就会带），不算新增负担。

    验签之外还做字段清洗 —— 签名只保证「内容没被改」，不保证「内容是合理
    的」。管理员手滑填了个 300 字的群名，签名照样有效，界面照样崩。
    """
    w = str(wire or "").strip()
    if not w:
        return None, "空内容"
    if "." not in w:
        return None, "格式不对（缺签名段）"
    left, _, right = w.partition(".")
    try:
        raw = _b64d(left)
        sig = _b64d(right)
    except Exception:                                          # noqa: BLE001
        return None, "base64 解码失败"

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        pk = Ed25519PublicKey.from_public_bytes(_b64d(PUBLIC_KEY_B64))
        pk.verify(sig, raw)
    except InvalidSignature:
        # 这一条是核心：签名对不上，一个字段都不采信。
        return None, "签名校验失败"
    except Exception as e:                                     # noqa: BLE001
        # cryptography 缺失 / 公钥本身被改坏，都落到这里
        return None, f"验签不可用：{e}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "内容不是合法 JSON"
    if not isinstance(payload, dict):
        return None, "内容不是对象"

    return _sanitize(payload)


def _sanitize(payload: dict) -> tuple[Optional[dict], str]:
    """字段清洗。签名有效也不代表内容能用。"""
    group = str(payload.get("group") or "").strip()
    if not _RE_GROUP_NO.match(group):
        return None, f"群号格式不对：{group!r}"

    name = _BAD_CHARS.sub("", str(payload.get("name") or "")).strip()[:MAX_NAME]
    tip = _BAD_CHARS.sub("", str(payload.get("tip") or "")).strip()[:MAX_TIP]

    # 群名空了就等于没名字，退回默认；话术空了是合理的（不想显示引导语），
    # 但 UI 那边要能处理空串，所以这里保留空串，不填默认。
    if not name:
        name = DEFAULT_GROUP_NAME

    return {
        "group": group,
        "name": name,
        "line": f"{name} {group}",
        "url": _join_url(group),
        "tip": tip,
        "src": "remote",
        "at": str(payload.get("at") or ""),
    }, ""


# ---------------------------------------------------------------- 配置 / 缓存

def _cfg(data_dir: Path) -> dict:
    try:
        d = json.loads((Path(data_dir) / CFG_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def base_url(data_dir: Path) -> str:
    """跟版本检查共用同一个域名。"""
    u = str(_cfg(data_dir).get("license_url") or
            "http://127.0.0.1:8080/api/license").strip()
    return u.rstrip("/")


def _cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / CACHE_FILE


def load_cache_raw(data_dir: Path) -> str:
    """读缓存原文（`payload.sig`）。不验签，验签在调用方做。"""
    try:
        return _cache_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_cache(data_dir: Path, wire: str) -> None:
    """写缓存。失败静默 —— 缓存只是省一次网络，写不进去不影响正确性。

    原子写：临时文件 + os.replace。中途断电最多丢缓存，不会留下半个文件
    让下次读取解密失败。
    """
    p = _cache_path(data_dir)
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".brand.tmp{os.getpid()}")
        tmp.write_text(wire, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 网络

def _ctx_for(url: str, data_dir: Path) -> object:
    """https 时给出 SSL context。默认走系统 CA 正常校验。

    以前这里无条件 CERT_NONE（因为服务端自签）。问题在于「无条件」：
    以后换成正式证书的 https，这段代码还在关校验，中间人换掉签名的
    上游响应就只是白名单升级成「真的能改」了。虽然 payload 本身还有
    Ed25519 兜着，但没必要留这个口子。

    现在改成 opt-in：config.json 写 "tls_insecure": true 才降级。
    """
    if not url.startswith("https://"):
        return None
    import ssl
    ctx = ssl.create_default_context()
    if bool(_cfg(data_dir).get("tls_insecure")):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, data_dir: Optional[Path] = None) -> tuple[Optional[str], str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "XiaoXiongMaoPoJia/1.3",
                      "Accept": "text/plain"})
    ctx = _ctx_for(url, Path(data_dir)) if data_dir is not None else None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.read().decode("utf-8", errors="replace").strip(), ""
    except urllib.error.HTTPError as e:
        # 404 = 服务端还没上线这个接口。按「没配远端」处理，不报错也不报警。
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"连不上（{e.reason}）"
    except (OSError, ValueError) as e:
        return None, f"请求失败：{e}"


def _gap_ok() -> bool:
    with _LOCK:
        return (time.time() - _LAST_HIT) >= MIN_GAP


def _mark_hit() -> None:
    global _LAST_HIT
    with _LOCK:
        _LAST_HIT = time.time()


# ---------------------------------------------------------------- 对外接口

def active() -> dict:
    """当前生效的引流配置。永远返回可用结构，调用方不用判空。"""
    with _LOCK:
        if _ACTIVE:
            return dict(_ACTIVE)
    return default()


def tamper() -> str:
    """上次验签失败的原因。空串 = 正常。界面据此显示异常提示。"""
    with _LOCK:
        return _TAMPER


def status() -> str:
    """builtin / remote / cached / tamper / offline。"""
    with _LOCK:
        return _LAST_STATUS


def apply_remote(wire: str) -> tuple[bool, str]:
    """验一条远端内容并切过去。返回 (成功, 说明)。

    失败时**不改变当前生效配置** —— 已经在用远端好数据的客户端，不能因为
    后来一次投毒就掉回默认值（那样攻击者只要持续投毒就能让引流卡消失）。
    """
    global _ACTIVE, _TAMPER, _LAST_STATUS

    payload, err = verify_wire(wire)
    if payload is None:
        with _LOCK:
            _TAMPER = err
            # 只有原本已经从远端拿到过东西，才把状态标成 tamper。
            # 从没连通过的情况下验签失败多半是服务端没配私钥，不算篡改。
            if _LAST_STATUS in ("remote", "cached"):
                _LAST_STATUS = "tamper"
        return False, err

    with _LOCK:
        _ACTIVE = payload
        _TAMPER = ""
        _LAST_STATUS = "remote"
    return True, "已更新"


def refresh(data_dir: Path, force: bool = False) -> dict:
    """拉一次远端配置。永远返回当前生效值，不抛异常。

    顺序：
      1. 内存里已有远端值 → 直接返回（force 才继续）
      2. 节流窗口内 → 直接用内存 / 缓存 / 默认
      3. 打网络 → 验签 → 成功就用，失败就用已有的
      4. 网络失败 → 退回缓存（缓存也要验签，被改过一样不认）

    返回的 dict 带 `src` 字段，界面据此区分「远端下发」和「内置默认」。
    """
    # global 声明必须在函数内任何使用之前 —— Python 在编译期就把函数体里
    # 所有对 _ACTIVE 的赋值点收集起来，一处用了局部名、后面再声明 global
    # 就是 SyntaxError，整个模块 import 失败。
    global _ACTIVE, _TAMPER, _LAST_STATUS

    with _LOCK:
        have = bool(_ACTIVE) and _ACTIVE.get("src") == "remote"
    if have and not force:
        return active()

    if not force and not _gap_ok():
        return active()

    _mark_hit()
    wire, err = _get(base_url(data_dir) + "/brand", data_dir)

    if wire is None:
        # 网络问题。用缓存（仍要验签）或内存里的旧值。
        cached = load_cache_raw(data_dir)
        if cached:
            payload, verr = verify_wire(cached)
            if payload is not None:
                with _LOCK:
                    if not _ACTIVE:
                        _ACTIVE = payload
                    _LAST_STATUS = "cached"
                    _TAMPER = ""
                return active()
            # 缓存被改过 —— 这比网络失败严重，要报出来
            with _LOCK:
                _TAMPER = f"本地缓存{verr}"
                _LAST_STATUS = "tamper"
            return active()
        with _LOCK:
            if _LAST_STATUS not in ("remote", "cached", "tamper"):
                _LAST_STATUS = "offline"
        return active()

    ok, why = apply_remote(wire)
    if ok:
        _save_cache(data_dir, wire)
        return active()

    # 服务端返回了内容但验不过。可能是没配私钥（服务端返回空）、也可能是投毒。
    # 两种情况都退回「当前已有的值」，并把原因记下来给界面看。
    with _LOCK:
        if not _ACTIVE:
            # 从没成功过，用内置默认值，但状态要标出来
            _LAST_STATUS = "tamper"
            _TAMPER = why
        else:
            _TAMPER = why
    return active()


# ---------------------------------------------------------------- 自检

def verify() -> list[str]:
    """内置默认值的自检。返回问题列表，空 = 正常。

    只查内置常量本身（这部分写死在 exe 里）；远端内容由验签兜着，不走这里。

    注意不要在这里写「群号必须等于 100000000」这种断言。发布包是打包时
    注入真号的（`tools/make_brand.py`），源码仓里才是占位值 —— 断死某一个
    号码会让注入后的包自检失败，用户看到「[异常] 群号被改过」而其实一切正常。
    要查的是**形状**：格式对不对、名字空不空、公钥长度对不对。
    """
    bad: list[str] = []
    if not _RE_GROUP_NO.match(DEFAULT_GROUP_NO):
        bad.append(f"群号格式不对：{DEFAULT_GROUP_NO!r}（应为 5-12 位数字）")
    if not DEFAULT_GROUP_NAME.strip():
        bad.append("群名是空的")
    if len(DEFAULT_GROUP_NAME) > MAX_NAME:
        bad.append(f"群名超长：{len(DEFAULT_GROUP_NAME)} 字（上限 {MAX_NAME}）")
    if len(DEFAULT_TIP) > MAX_TIP:
        bad.append(f"话术超长：{len(DEFAULT_TIP)} 字（上限 {MAX_TIP}）")
    if len(_b64d(PUBLIC_KEY_B64)) != 32:
        bad.append("内置公钥不是 32 字节")
    return bad


# ---------------------------------------------------------------- 兼容别名
#
# 1.3.0 早期这几个是模块级常量（GROUP_NO / GROUP_NAME / LINE / JOIN_URL /
# TIP），app.py 和 CLI 有十几处直接读。改成函数式之后如果把名字删掉，那些
# 调用点在 import 阶段就 AttributeError —— 用户看到的是双击没反应。
#
# 用 PEP 562 的模块级 __getattr__ 做懒代理：`brand.LINE` 每次访问都取当前
# 生效值，远端配置生效后自动跟着变。

_ALIAS = {
    "GROUP_NO": "group",
    "GROUP_NAME": "name",
    "LINE": "line",
    "JOIN_URL": "url",
    "TIP": "tip",
}


def __getattr__(name: str):
    """模块级懒代理。只处理上面那五个别名，其余照常 AttributeError。"""
    key = _ALIAS.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    v = active()[key]
    # group / name / line / url 一定是字符串；tip 可能是空串，也是字符串
    return v

