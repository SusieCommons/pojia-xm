# -*- coding: utf-8 -*-
"""版本检查。后台发布新版本 → 客户端启动时提示 / 强制更新。

为什么要有这层
--------------
以前版本固定在 exe 里，发新版只能靠群里喊一声「重新下载」。用户手上
留着一堆旧版，出了问题也复现不了 —— 你修的是 1.3.0 的 bug，他跑的是 1.2.2。

现在后台有个「版本」页签，填版本号 + 下载地址 + 是否强制，保存即生效，
不用重启服务。客户端每次启动问一次，有新版就顶一条横幅；强制的话横幅
关不掉，点「稍后」也会再弹。

设计约束
--------
1. **绝不阻塞启动。** 检查在后台线程跑，最多等 4 秒，失败就当没有新版。
   网络不通、服务端宕机、DNS 挂了都不能让用户打不开工具。
2. **缓存只用于兜底，不用于「确认最新」。** 这一点是 1.3.0 改的：
   以前缓存 6 小时，结果是后台发布了新版，最长 6 小时用户才看见 ——
   跟「我在后台说更新了就要更新」直接矛盾。现在每次启动都会真的问一次
   服务端；缓存只在网络失败时顶上，让界面上还能显示「上次已知有 1.3.2」。
   代价是每次启动多一个 4 秒上限的后台请求，用户在界面上感觉不到。
3. **问得再勤也要有下限。** 同一次进程里 60 秒内不重复打接口（界面反复
   开关、点「检查更新」连点都不会打出请求风暴）；跨进程只看启动次数，
   不设时间窗，因为用户重开一次就是一次明确意愿。
4. **不自动下载。** 只给链接和哈希，让用户自己点。自动替换正在运行的
   exe 在 Windows 上要额外的重命名 + 重启逻辑，风险大于收益。
5. **强制更新也要有一条退路。** force 时横幅不给关闭按钮，但保留
   「这次先用旧版」—— 万一新版在某人机器上起不来，他还能退回旧版自救。
6. **服务端挂掉不能封死人。** 网络失败一律按「没有强制更新」处理 ——
   拿不到版本号就锁功能，等于把服务端可用性变成了产品的可用性上限。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

#: 授权/更新服务端根地址。跟 config.json 的 license_url 共用（历史上就这一个域名）。
DEFAULT_URL = "http://127.0.0.1:8080/api/license"

#: 单次请求超时。检查更新不该让用户等。
TIMEOUT = 4.0

CACHE_FILE = "update.json"
CFG_FILE = "config.json"

#: 同一进程内两次真实请求的最小间隔（秒）。防的是界面反复触发，
#: 不是防用户重开 —— 重开是新进程，这个计数器归零。
MIN_GAP = 60

#: 缓存多久之后算「过期」。只影响界面是否显示「数据可能不准」，
#: 不再影响「要不要去问服务端」—— 那个现在每次都问。
CACHE_TTL = 6 * 3600

_RE_VER = re.compile(r"^\d+(\.\d+){0,3}$")

#: 本进程上次真打网络的时间戳。0 = 还没打过。
_LAST_HIT = 0.0
_LAST_LOCK = threading.Lock()

#: 上次「响应验签没过」的原因。空 = 正常。
#: 这个跟 brand 的 tamper 是一个性质：非空表示**有人改过服务端响应**，
#: 界面要能看见，不能当成普通网络失败静默吞掉。
_TAMPER = ""

#: 版本响应参与签名的字段。跟服务端 release_payload() 的 payload 逐一对应。
#: 改这里必须同步改服务端，否则线上所有响应都验不过 —— 表现是「永远没有
#: 新版」，不会报错，排查会很痛苦。
VERIFY_FIELDS = ("ver", "url", "notes", "force", "sha256", "size", "at")


def _gap_ok() -> bool:
    """现在允许打网络吗（同进程节流）。"""
    with _LAST_LOCK:
        return (time.time() - _LAST_HIT) >= MIN_GAP


def _mark_hit() -> None:
    global _LAST_HIT
    with _LAST_LOCK:
        _LAST_HIT = time.time()


# ---------------------------------------------------------------- 版本号

def ver_tuple(s: str) -> tuple[int, ...]:
    """把 "1.2.4" 变成 (1,2,4)，用于比较。

    允许前缀 `v`、允许 `1.2.4-beta` 这种尾巴（取到非数字就停）。
    末尾的 0 去掉，这样 "1.2" 和 "1.2.0" 相等。
    """
    out: list[int] = []
    for part in re.split(r"[.\-_+]", str(s or "").strip().lstrip("vV")):
        m = re.match(r"(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    while out and out[-1] == 0:
        out.pop()
    return tuple(out)


def newer(remote: str, local: str) -> bool:
    """远端是否比本地新。任一侧为空 / 不合法时返回 False。"""
    r = str(remote or "").strip()
    if not r or not _RE_VER.match(r.lstrip("vV")):
        return False
    return ver_tuple(r) > ver_tuple(local)


# ---------------------------------------------------------------- 验签
#
# 版本响应为什么要验签
# --------------------
# 这条数据的用途是「让用户去下载并运行一个 exe」。不验签的话，一次 HTTP
# 中间人就能把 force 改成 true、把 url 换成自己的马 —— 用户看到的是
# 「必须更新才能用」，点下去装的是别人的东西。
#
# 复用 core/brand.py 那把公钥（同一对密钥，服务端也只有一份 SIGNER_KEY），
# 这样不用再多管一份密钥材料。canon() 也必须跟 brand 完全一致。


def canon(payload: dict) -> bytes:
    """跟 core/brand.py:canon() 和 server.py:canon() 逐字节一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _b64d(s: str) -> bytes:
    import base64
    t = str(s or "").strip().replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t)


def verify_wire(wire: str) -> tuple[Optional[dict], str]:
    """验一条版本签名。返回 (payload, 错误说明)。任一为 None 表示不可采信。

    公钥从 core.brand 取 —— 单一真源，改密钥只改一处。
    """
    w = str(wire or "").strip()
    if not w:
        return None, "响应没有签名"
    if "." not in w:
        return None, "签名格式不对"
    left, _, right = w.partition(".")
    try:
        raw = _b64d(left)
        sig = _b64d(right)
    except Exception:                                          # noqa: BLE001
        return None, "签名段解码失败"

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from core import brand
        pk = Ed25519PublicKey.from_public_bytes(_b64d(brand.PUBLIC_KEY_B64))
        pk.verify(sig, raw)
    except InvalidSignature:
        return None, "签名校验失败"
    except Exception as e:                                     # noqa: BLE001
        return None, f"验签不可用：{e}"

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "签名内容是坏 JSON"
    if not isinstance(payload, dict):
        return None, "签名内容不是对象"

    # 字段类型收敛：签名只保证没被改，不保证字段类型合理。
    out = {}
    for k in VERIFY_FIELDS:
        out[k] = payload.get(k)
    out["ver"] = str(out["ver"] or "").strip().lstrip("vV")
    out["url"] = str(out["url"] or "")
    out["notes"] = str(out["notes"] or "")[:800]
    out["force"] = bool(out["force"])
    out["sha256"] = str(out["sha256"] or "").lower()
    try:
        out["size"] = int(out["size"] or 0)
    except (TypeError, ValueError):
        out["size"] = 0
    out["at"] = str(out["at"] or "")
    return out, ""


def tamper() -> str:
    """上次版本响应验签失败的原因。空 = 正常。"""
    global _TAMPER
    with _LAST_LOCK:
        return _TAMPER


def _set_tamper(s: str) -> None:
    global _TAMPER
    with _LAST_LOCK:
        _TAMPER = s


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


# ---------------------------------------------------------------- 缓存

def _path(data_dir: Path) -> Path:
    return Path(data_dir) / CACHE_FILE


def load_cache(data_dir: Path) -> dict:
    try:
        d = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(data_dir: Path, rec: dict) -> None:
    p = _path(data_dir)
    tmp: Optional[Path] = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 临时名带 pid：界面手动检查 + 后台轮询可能同时写
        tmp = p.with_name(f".update.tmp{os.getpid()}_{threading.get_ident()}")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 请求

def _ctx_for(url: str, data_dir: Optional[Path] = None) -> object:
    """https 时给出 SSL context。默认走系统 CA 正常校验。

    以前这里是 `ctx.verify_mode = ssl.CERT_NONE` —— 服务端用自签证书，
    关掉校验最省事。但那个设置是**无条件**生效的：哪天把地址换成正式
    证书的 https，这段代码还在静默关校验，中间人换 exe 一点阻力都没有。

    现在改成 opt-in：只有 config.json 显式写 `"tls_insecure": true` 才降级
    （自签自建的场景自己承担风险），默认严格校验。
    """
    if not url.startswith("https://"):
        return None
    ctx = ssl.create_default_context()
    insecure = False
    if data_dir is not None:
        insecure = bool(_cfg(data_dir).get("tls_insecure"))
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, data_dir: Optional[Path] = None) -> tuple[Optional[dict], str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "XiaoXiongMaoPoJia/1.3",
                      "Accept": "application/json"})
    ctx = _ctx_for(url, data_dir)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return json.loads(raw), ""
    except urllib.error.HTTPError as e:
        # 404 = 服务端还没上线这个接口，按「没有新版」处理，不报错
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"连不上更新服务（{e.reason}）"
    except (OSError, ValueError) as e:
        return None, f"请求失败：{e}"


def _norm(j: dict, local: str) -> dict:
    """把服务端返回（或本地缓存）整成界面能直接用的结构。"""
    ver = str(j.get("ver") or "").strip().lstrip("vV")
    has = bool(ver) and newer(ver, local)
    return {
        "has": has,
        "ver": ver,
        "local": local,
        "url": str(j.get("url") or ""),
        "notes": str(j.get("notes") or "")[:800],
        "force": bool(j.get("force")) and has,
        "sha256": str(j.get("sha256") or "").lower(),
        # size 给界面显示「12.3 MB」，服务端没填就 0
        "size": int(j.get("size") or 0),
        "at": str(j.get("at") or ""),
        "check_at": int(time.time()),
        "err": "",
        # 缓存里不存 tamper：它是「本次响应」的属性，不是内容的属性。
        "tamper": "",
        # 这条结论从哪来：remote = 本次联网且验签过；cache = 退回缓存
        # 但**重新验签过**。两者可信度一样，只是提醒用户「这是上次的」。
        "src": "remote",
    }


def _from_cache(data_dir: Path, local: str, err: str = "") -> Optional[dict]:
    """退回缓存，但必须重新验过 wire 才算数。

    为什么不能直接 `_norm(load_cache(...))`
    ------------------------------------
    `update.json` 是磁盘上的普通 JSON，任何能写用户目录的程序都能改它。
    如果断网时直接采信里面的 `sha256`，攻击者只要把这个字段换成自己那个
    恶意 exe 的哈希，客户端就会把「下载地址 + 哈希」一起显示成官方发布
    —— 更糟的是 `verify-download` 会拿这个被换掉的哈希去比对用户的文件，
    把他手里的恶意包判成「与签名一致」。

    换句话说：不重新验签的缓存回退，等于把校验标准交给攻击者。所以这里
    只认缓存里的 **原始 wire**，验过才用；平铺字段一概不读。

    返回 None = 缓存不可用（没缓存 / 没 wire / 验签挂），调用方自己决定
    怎么报错。验签挂时会留下 tamper 记录，界面能区分「没网」和「被改」。
    """
    c = load_cache(data_dir)
    if not c.get("check_at"):
        return None
    blob = str(c.get("wire") or "")
    if not blob:
        # 1.3.2 及更早写的缓存没有 wire。宁可当作「没有信息」等下一次
        # 联网，也不能拿它当依据 —— 它的字段从来没有来源证明。
        return None
    payload, verr = verify_wire(blob)
    if payload is None:
        _set_tamper(f"本地缓存签名无效：{verr}")
        return None
    r = _norm(payload, local)
    r["src"] = "cache"
    r["stale"] = True
    r["pending"] = False
    if err:
        r["err"] = err
    return r


def empty(local: str, err: str = "") -> dict:
    """没查到时的空结构。字段跟 _norm() 对齐，前端不用判空。"""
    return {"has": False, "ver": "", "local": local, "url": "", "notes": "",
            "force": False, "sha256": "", "size": 0, "at": "",
            "check_at": 0, "err": err, "pending": True, "tamper": "",
            "src": ""}


def cached(data_dir: Path, local: str) -> dict:
    """只读本地缓存，绝不联网。boot() 用这个 —— 启动路径上一个 socket
    都不能发，DNS 超时能把窗口卡在「正在加载」四秒。

    这是**过渡占位**，不是最终结论：调用方拿到后应该看 pending 字段，
    等 warm_result() 给出真值再定横幅。

    注意这里也要验签（走 `_from_cache`）。启动路径不联网 ≠ 可以信任
    磁盘。以前这里是 `_norm(load_cache(...))`，等于开机第一眼就把未验签
    的缓存当成官方发布显示出去。
    """
    r = _from_cache(data_dir, local)
    if r is None:
        return empty(local)
    # stale 只在「这确实是旧数据」时有意义。启动占位不算旧 —— 它马上
    # 就会被真实结果覆盖。标上会让前端在几百毫秒里闪一下「数据可能过期」。
    r["stale"] = False
    r["pending"] = False
    return r


def check(data_dir: Path, local: str, force: bool = False) -> dict:
    """查有没有新版。永远返回 dict，不抛异常。

    force=False：受 MIN_GAP 节流 —— 同一进程 60 秒内第二次调用直接回缓存，
                 不打网络。启动 + 前端轮询会连着调好几次，不节流就是三次
                 请求。
    force=True ：无视节流，一定打网络。用户手动点「检查更新」走这条，
                 后台刚发完版想立刻验证也走这条。

    网络失败时退回缓存并带 err，界面据此显示「检查失败，可能是 1.3.2」，
    而不是把横幅收起来装作没事。

    缓存回退一律走 `_from_cache`（重新验签）。这里以前直接 `_norm(cache)`，
    等于断网时把校验标准交给磁盘上的普通 JSON。
    """
    if not force and not _gap_ok():
        r = _from_cache(data_dir, local)
        if r is not None:
            r["cached"] = True
            # 节流期内复用同一份结论，不算「旧数据」
            r["stale"] = False
            return r
        return empty(local)

    _mark_hit()
    j, err = _get(base_url(data_dir) + "/version", data_dir)
    if j is None:
        # 网络问题：退回缓存（如果有且验签过），带上错误说明给界面显示
        r = _from_cache(data_dir, local, err)
        if r is not None:
            return r
        return {"has": False, "ver": "", "local": local, "url": "", "notes": "",
                "force": False, "sha256": "", "size": 0, "at": "",
                "check_at": int(time.time()), "err": err, "pending": False,
                "tamper": tamper(), "src": ""}

    if not j.get("ok", True):
        return {"has": False, "ver": "", "local": local, "url": "", "notes": "",
                "force": False, "sha256": "", "size": 0, "at": "",
                "check_at": int(time.time()), "pending": False,
                "err": str(j.get("msg") or "服务端拒绝")}

    # ---- 验签闸门
    #
    # 有 wire 就必须验过才采信。验不过三种处理，区别很重要：
    #   · 从没成功验过签 → 当作「没有新版」，并标记 tamper，界面提示异常。
    #     不能拿未验签的内容去提示用户下载 exe。
    #   · 之前验过、这次验不过（投毒）→ 同上，但保留 tamper 记录。
    # 绝不「验不过就当普通网络失败退回缓存继续显示旧版」—— 那样中间人只要
    # 持续投毒就能让强制更新永远不生效，也能让用户永远看不到真新版。
    wire = str(j.get("wire") or "")
    if wire:
        payload, verr = verify_wire(wire)
        if payload is None:
            _set_tamper(verr)
            r = empty(local, f"版本信息校验失败：{verr}")
            r["pending"] = False
            r["tamper"] = verr
            return r
        _set_tamper("")
        r = _norm(payload, local)
    else:
        # 服务端没配私钥。宁可不提示更新，也不能采信这条无来源的消息。
        why = str(j.get("sign_err") or "服务端未签名")
        _set_tamper(why)
        r = empty(local, f"版本信息不可信：{why}")
        r["pending"] = False
        r["tamper"] = why
        return r

    r["pending"] = False
    _save_cache(data_dir, {
        "ver": r["ver"], "url": r["url"], "notes": r["notes"],
        "force": r["force"], "sha256": r["sha256"], "size": r["size"],
        "at": r["at"], "check_at": r["check_at"],
        # 原始 wire 一起存。
        #
        # 平铺字段是可改的普通 JSON，单独拿它做任何判断都等于信任磁盘上
        # 那份文件。存下 wire 之后，离线（或服务端挂了）时还能重新验一次
        # 签名再信 —— 校验标准仍是那把公钥，不是磁盘。
        #
        # 代价是缓存大几十字节（wire 约 300-600 B）。_norm() 只挑它认识的
        # 键，多存一个不会影响界面。
        "wire": wire,
    })
    return r


# ---------------------------------------------------------------- 后台预热
#
# 界面打开时先读缓存（cached），同时在后台打一次网络（warm）。
# 前端 500ms 后自己来取 warm_result()，谁先到都行 —— 网络快的话用户
# 还没看完客户端列表，更新横幅就已经填好了。
#
# 注意：warm 以前会看「缓存还新鲜就跳过」。1.3.0 去掉了这个判断 ——
# 缓存新鲜只说明上次问过，不代表后台这几小时没发新版。用户的要求是
# 「后台说更新就更新」，那就每次启动都真的问一次，靠 MIN_GAP 防风暴。

_WARM: dict = {}
_WARM_LOCK = threading.Lock()
_WARM_T: Optional[threading.Thread] = None


def warm(data_dir: Path, local: str) -> None:
    """后台线程查一次。重复调用只起一个线程。

    起线程前先把缓存塞进 _WARM：前端第一次轮询就能拿到「上次已知」的
    结果，横幅该显示就显示，不用等网络那几百毫秒。
    """
    global _WARM_T
    with _WARM_LOCK:
        if _WARM_T is not None and _WARM_T.is_alive():
            return
        # 预热占位也走验签版缓存。这里以前是 `_norm(load_cache(...))`，
        # 前端第一次轮询（500ms）就能拿到它 —— 未验签的缓存会在这个
        # 时间窗口里被当成官方发布显示，比 cached() 那条更容易被看到。
        base = _from_cache(data_dir, local)
        if base is not None:
            base["pending"] = True      # 有值但还在查新的
            base["stale"] = True
            _WARM.clear()
            _WARM.update(base)
        else:
            _WARM.clear()

        def go():
            try:
                r = check(data_dir, local, force=True)
            except Exception as e:                             # noqa: BLE001
                r = empty(local, f"检查失败：{e}")
            r["pending"] = False
            with _WARM_LOCK:
                _WARM.clear()
                _WARM.update(r)

        _WARM_T = threading.Thread(target=go, daemon=True, name="xm-update")
        _WARM_T.start()


def warm_result() -> dict:
    """取后台预热的结果。还没跑完就回 pending=True，前端据此决定要不要再问。"""
    with _WARM_LOCK:
        return dict(_WARM) if _WARM else {}


def warm_busy() -> bool:
    """后台那个线程还在跑吗。前端轮询到 pending 时用来决定超时话术。"""
    with _WARM_LOCK:
        return _WARM_T is not None and _WARM_T.is_alive()
