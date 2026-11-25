#!/usr/bin/env python3
"""小熊猫破甲 · 应用入口（GUI + CLI）。

    xm.exe                          打开窗口
    xm.exe status                   只看状态
    xm.exe apply                    给所有已装客户端注入
    xm.exe apply -t codex           只注入 Codex
    xm.exe revert                   还原到注入前
    xm.exe apply --dry              只算不写

窗口用 WebView2（系统自带）渲染前端；万一 WebView2 不可用，自动退到
Edge --app 模式起同一个界面，功能一致。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import LABELS, ORDER, all_targets, resolve  # noqa: E402
from core import REGISTRY  # noqa: E402
from core import brand  # noqa: E402
from core import licnet  # noqa: E402
from core import proc  # noqa: E402
from core import ticket  # noqa: E402
from core import update  # noqa: E402
from core.zh_argparse import apply_zh  # noqa: E402
from core import license as lic  # noqa: E402
from core import base as core_base  # noqa: E402
from core.base import (DATA_DIR, list_backups, load_state,  # noqa: E402
                       prune_backups, read_text, restore_backup, save_root)

APP_NAME = "小熊猫破甲"
VERSION = "1.3.8"
WIN_TITLE = APP_NAME


# ---------------------------------------------------------------- 使用前须知

#: 条款版本。改一次条款就把这个号 +1，老用户会被重新问一遍 —— 不能因为
#: 以前同意过旧条款，就当他同意新条款。
CONSENT_VERSION = "1"
CONSENT_FILE = DATA_DIR / "consent.json"

#: 唯一一份条款正文。GUI 直接渲染这个结构，CLI 打印同一份 —— 两处不会说
#: 不一样的话。
DISCLAIMER = [
    ("用途限定", [
        "本工具用于你本人拥有或已获明确授权的设备：对本地 AI 客户端的配置文件"
        "做备份、替换与还原。目的是安全研究、功能测试与个人效率配置。",
        "允许的典型用法：研究提示词工程、观察不同人格配置对模型输出的影响、"
        "验证配置注入与回滚机制、学习文件级持久化与恢复的实现方式。",
    ]),
    ("禁止用途", [
        "不得将本工具用于任何违法犯罪活动。包括但不限于：未经授权侵入、控制"
        "他人计算机信息系统；窃取、篡改、删除他人数据；制作或传播计算机病毒、"
        "木马等恶意程序；实施诈骗、敲诈、骚扰、人肉搜索；侵犯他人知识产权或"
        "个人隐私；规避国家规定的网络安全管理制度。",
        "不得用于探测、扫描、攻击任何你未获授权的目标。",
    ]),
    ("合法性与责任", [
        "你有责任自行确认：对所操作的设备、软件、文件拥有合法权限；使用行为"
        "符合所在地法律法规与所在单位的管理规定。",
        "因使用或无法使用本工具产生的一切后果——包括数据丢失、软件损坏、账号"
        "受限或封禁、第三方索赔、行政处罚及刑事责任——均由使用者自行承担。",
    ]),
    ("第三方软件", [
        "修改第三方客户端的配置文件可能违反其用户协议，可能导致账号被限制、"
        "降权或永久封禁。是否承担该风险由你自行判断。",
        "本工具与所列客户端厂商无任何关联，未获其授权、认可或背书。",
    ]),
    ("无担保", [
        "本工具按「现状」提供，不附带任何明示或默示担保，包括适销性、特定用途"
        "适用性与不侵权担保。",
        "工具会在修改前自动备份，但备份不保证一定能完整恢复。使用前请自行备份"
        "重要数据。",
    ]),
    ("条款接受", [
        "点击「同意并继续」表示你已完整阅读、理解并接受上述全部条款，并自愿承担"
        "相应责任。",
        "不接受则无法使用本工具的任何功能。",
    ]),
]


def consent_state() -> dict:
    """当前这台机器同意过没有。版本对不上也算没同意。"""
    try:
        d = json.loads(CONSENT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    ok = bool(d.get("accepted")) and str(d.get("version")) == CONSENT_VERSION
    return {"accepted": ok, "at": d.get("at", "") if ok else "",
            "version": CONSENT_VERSION, "sections": DISCLAIMER}


def save_consent() -> dict:
    """落一条同意记录。写临时文件再替换，避免半份 JSON。"""
    rec = {
        "accepted": True,
        "version": CONSENT_VERSION,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": os.environ.get("COMPUTERNAME", ""),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONSENT_FILE.with_name(".consent.xmtmp")
    try:
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONSENT_FILE)
    except OSError:
        pass
    return rec


def disclaimer_text() -> str:
    """给控制台用的纯文本版。段落按 76 字折行，GBK 控制台也不会截断。"""
    out = [f"【{APP_NAME} · 使用前须知】", ""]
    for i, (head, paras) in enumerate(DISCLAIMER, 1):
        out.append(f"{i}. {head}")
        for p in paras:
            out.append("   " + p)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- 授权（已取消）
#
# 1.3.0 起不再有卡密授权：任何人下载即用。
#
# 下面几个函数全是兼容壳。保留是因为界面、CLI、守护进程都还在调它们，
# 一次性删签名要改十几个调用点，回归面太大 —— 而一旦漏一处，用户拿到的
# 是「双击没反应」，连报错都看不到。所以改成无条件放行，函数名不动。
#
# 现在真正跟服务端通信的只剩版本检查（core/update.py），它不拦人，只提示。

def license_state(force: bool = False) -> dict:
    """界面用的状态。现在恒定是「可用」。"""
    return {"kind": "ok", "machine": lic.machine_code(), "expire": "永久",
            "note": "", "msg": "无需授权", "server": licnet.base_url(DATA_DIR),
            "offline": False, "hint": "直接可用", "ticket": _ticket_info()}


def license_ok(force: bool = False) -> bool:
    """闸门。永远通过。

    保留函数是因为 apply / verify / watch / restore 四条路径都在调它。
    真要做「按版本封禁」之类的事，改这里一处即可 —— 但那是产品决策，
    不是默认行为。版本过低时的提示由 update.check() 的 force 字段负责，
    它只提示，不拦人。
    """
    return True


def _ticket_gate(force: bool = False) -> bool:
    return True


def license_hint() -> str:
    return "直接可用 · 无需激活"


def _ticket_info(force: bool = False) -> dict:
    return {"required": False, "ok": True, "left": 0, "msg": "无需票据"}


# ---------------------------------------------------------------- 更新 / 引流

def update_check(force: bool = False, nonblock: bool = False) -> dict:
    """查有没有新版。永不抛异常 —— 检查更新失败不该让界面报错。

    nonblock=True 时只读本地缓存，同时（如果还没查过）在后台发起一次
    网络请求。启动路径必须走这条：同步打网络最坏是 4 秒，用户看到的就是
    双击之后四秒白屏。

    返回的 pending 字段含义：True = 这只是占位值，前端应继续轮询
    update_poll() 取真值。

    `tamper` 字段：非空表示服务端响应验签没过（有人在改这条消息）。
    界面要能看到，不能当成普通网络失败 —— 后者只是「这次没问到」，
    前者是「有人想让我装别的东西」。
    """
    try:
        if nonblock:
            r = update.warm_result()
            if r:
                return r
            update.warm(DATA_DIR, VERSION)
            r = update.warm_result()
            if r:
                # warm() 起线程前已经把缓存塞进来了，多半能立刻拿到
                return r
            r = update.cached(DATA_DIR, VERSION)
            r["pending"] = True
            return r
        return update.check(DATA_DIR, VERSION, force=force)
    except Exception as e:                                     # noqa: BLE001
        return {"has": False, "ver": "", "local": VERSION, "url": "",
                "notes": "", "force": False, "sha256": "", "size": 0,
                "at": "", "check_at": 0, "err": f"检查失败：{e}",
                "pending": False, "tamper": ""}


def brand_info() -> dict:
    """引流信息。界面上那张卡直接渲染它。

    `src` 是 builtin / remote / cached 之一；`tamper` 非空表示「服务端下发
    的内容验签没过」，界面要在卡片上给一句可见提示 —— 静默换回默认值会让
    人以为是配置没生效，实际是有人在中间改内容。
    """
    d = brand.active()
    d["bad"] = brand.verify()
    d["tamper"] = brand.tamper()
    d["status"] = brand.status()
    return d


def brand_warm() -> None:
    """后台预热远端引流内容。启动路径不能等网络。

    4 秒超时的请求放在主线程上，用户看到的就是双击后白屏 —— 跟版本检查
    犯的是同一个错，所以这里同样甩到守护线程里。
    """
    def run() -> None:
        try:
            brand.refresh(DATA_DIR)
        except Exception:                                      # noqa: BLE001
            pass                      # 引流内容拉不到就用默认值，不该报错

    threading.Thread(target=run, name="brand-warm", daemon=True).start()


# ---------------------------------------------------------------- 强制更新闸门
#
# 后台把「强制更新」打开之后，旧版不能继续干活。这里缓存最近一次检查
# 结果，_start / verify / watch_toggle 都查它。
#
# 关键取舍：**只有服务端明确说 has+force 才拦**。网络不通、返回缺字段、
# 解析出错一律放行 —— 服务端挂掉不能让所有人打不开工具。

_UPD: dict = {}
_UPD_LOCK = threading.Lock()


def set_update_state(r: dict) -> dict:
    with _UPD_LOCK:
        _UPD.clear()
        _UPD.update(r or {})
    return r


def hard_block() -> tuple[bool, str]:
    """返回 (是否拦, 拦的话给用户看的话)。"""
    with _UPD_LOCK:
        r = dict(_UPD)
    if not r.get("has") or not r.get("force"):
        return False, ""
    ver = r.get("ver") or "新版本"
    return True, f"必须更新到 {ver} 才能继续使用 · 点「立即更新」下载"


# ---------------------------------------------------------------- 资源

def res(name: str) -> Path:
    """打包后资源在 _MEIPASS，源码运行时在脚本目录。"""
    base = Path(getattr(sys, "_MEIPASS", HERE))
    p = base / name
    if p.exists():
        return p
    return HERE / name


def load_html() -> str:
    p = res("ui/index.html")
    return p.read_text(encoding="utf-8") if p.exists() else (
        "<h1 style='font-family:sans-serif'>缺少 ui/index.html</h1>")


def load_persona(path: str | None = None) -> str:
    p = Path(path) if path else res("persona.md")
    if not p.exists():
        raise SystemExit(f"找不到人格文件：{p}")
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def crash_log(exc: str) -> None:
    try:
        d = Path(os.environ.get("TEMP", "."))
        (d / "xm-crash.log").write_text(exc, encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 手动指定目录

#: 每个客户端「该选哪个目录」的提示。弹选择框时从这儿起，也印在提示里。
SUGGEST = {
    "codex": ("~/.codex", "C:\\Users\\<你>\\.codex"),
    "wb": ("~/.workbuddy", "C:\\Users\\<你>\\.workbuddy"),
    "zcode": ("~/.zcode", "C:\\Users\\<你>\\.zcode"),
    "dsh": ("~/.dsh", "C:\\Users\\<你>\\.dsh"),
}


def _suggest_dir(name: str) -> str:
    """弹选择框时的起始目录。存在才给，避免框子弹到不存在的地方。

    这里必须用 `core_base.HOME`（已经逐级兜过底）而不是 `Path.home()` ——
    精筒系统上 `Path.home()` 会抛 RuntimeError，而这条路径在「用户双击
    要指定目录」时被调用，抛出去等于点了没反应。
    """
    home = core_base.HOME
    d = home / ("." + {"codex": "codex", "wb": "workbuddy",
                       "zcode": "zcode", "dsh": "dsh"}.get(name, name))
    try:
        return str(d) if d.is_dir() else str(home)
    except OSError:
        return str(home)


def dir_hint(name: str) -> str:
    """给界面用的一句话：这个客户端该选哪个目录。"""
    short, win = SUGGEST.get(name, ("", ""))
    return f"选 {win}" if win else ""


# ---------------------------------------------------------------- 探测

def probe(t, live: set[str] | None = None) -> dict:
    """把一个 Target 探成前端能直接渲染的字典。

    state: absent / unsupported / ready / partial / injected / error

    manual 字段是给界面用的：用户手动指定过目录没有、那个目录还有效没有。
    没有它，界面上「选错目录」和「压根没装」长得一模一样，用户只能反复
    双击、反复选、反复失败。
    """
    d = {"name": t.name, "label": LABELS.get(t.name, t.name),
         "installed": False, "path": "", "state": "absent",
         "detail": "未安装", "injectable": False, "injected": 0, "total": 0,
         "running": False, "manual": "", "manual_bad": ""}
    raw = ""
    try:
        raw = core_base.raw_root(t.name)
    except Exception:                                          # noqa: BLE001
        raw = ""
    d["manual"] = raw

    try:
        found = t.detect()
    except Exception as e:                                     # noqa: BLE001
        d.update(state="error", detail=f"探测失败：{e}")
        return d

    # 手动记过目录但这次没认出来 —— 把适配器给的原因带上去。
    # 不管自动探测有没有找到，都要记：用户的指定被忽略了这件事必须让他
    # 知道。只在「找不到」时才说，会出现这种情况 ——
    #   用户把目录选成 Program Files，适配器拒绝了，但自动探测在
    #   ~/.workbuddy 里找到了客户端，界面显示「已安装」，一切正常。
    #   用户以为自己的选择生效了，实际写的一直是自动探测的那个目录。
    note = getattr(t, "manual_note", "") or ""
    if raw and note and not getattr(t, "manual_used", False):
        d["manual_bad"] = note

    if not found:
        if raw:
            # 选过但无效：别再说「未安装 · 双击指定目录」—— 用户已经选过了
            d["detail"] = note or "指定的目录用不了，双击换一个"
        return d

    d["installed"] = True
    d["path"] = str(t.root) if t.root else ""
    if live and t.procs:
        d["running"] = any(p in live for p in t.procs)

    try:
        hits = t.collect()
    except Exception as e:                                     # noqa: BLE001
        d.update(state="error", detail=f"枚举靶点失败：{e}")
        return d

    if not hits:
        # 没有文件级靶点：把 status() 的原话给用户，比「不支持」有信息量
        raw = ""
        try:
            raw = t.status()
        except Exception:                                      # noqa: BLE001
            # status() 只是「补充说明」用的。适配器实现出错不该让整个
            # 客户端列表崩掉 —— 下面会用兜底文案「没有可写的靶点」。
            pass
        detail = _strip_prefix(raw, d["path"]) or "没有可写的靶点"
        d.update(state="unsupported", detail=detail)
        return d

    applied = 0
    for h in hits:
        try:
            if t.is_applied(h):
                applied += 1
        except Exception:                                      # noqa: BLE001
            # 单个靶点判定失败就当「没注入」，继续统计其余的。这里的
            # 语义是「数一数」，数不准不该让整个扫描报错 —— 真要精确
            # 判断有 verify() 那条路。
            pass

    total = len(hits)
    d.update(injectable=True, injected=applied, total=total)
    if applied == 0:
        # 子类可以给一句更具体的（比如「现有 pojia.persona.md，将由本工具接管」）
        hint = ""
        try:
            hint = t.hint()
        except AttributeError:
            # 老适配器没有 hint()，正常情况 —— 用下面的通用文案
            pass
        except Exception:                                      # noqa: BLE001
            # hint() 实现出错也不该让扫描失败，同上：只是补充说明
            pass
        d.update(state="ready", detail=hint or f"{total} 个靶点可写")
    elif applied >= total:
        d.update(state="injected", detail=f"已破甲 · {applied}/{total}")
    else:
        d.update(state="partial", detail=f"部分已破甲 · {applied}/{total}")
    return d


def _strip_prefix(s: str, path: str) -> str:
    if not s:
        return ""
    for pre in (f"已安装（{path}），", f"已安装({path}),"):
        if s.startswith(pre):
            return s[len(pre):]
    return s


def persona_brand_ok() -> tuple[bool, str]:
    """人格文件里的引流段和内置默认值对得上吗。

    两处各写一遍群号是有原因的：persona.md 是纯文本，注入到别人的客户端里
    之后跟程序彻底分离，只能写死；界面上那张卡又必须能从代码里读。既然是
    两份，就一定会有「改了 A 忘了 B」的那天 —— 改了群号重新打包，注入进去
    的还是老群号，用户加进一个没人管的群。

    所以启动时比一次。不一致不拦人（拦了用户什么也做不了），但要在日志和
    界面上说出来，让打包的人自己看见。

    比的是 **DEFAULT_*** 而不是 brand.GROUP_NO：1.3.0 起群号可以从后台下发，
    persona.md 里的那份不可能跟着变（它已经注进别人机器里的文件了）。拿
    当前生效值去比，后台一改群号就恒定报不一致 —— 那是假警报，会把真的
    「打包漏改」淹没掉。
    """
    text = ""
    try:
        text = res("persona.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "找不到 persona.md，无法校验引流段"
    if brand.DEFAULT_GROUP_NO not in text:
        return False, f"persona.md 里没有群号 {brand.DEFAULT_GROUP_NO}"
    if brand.DEFAULT_GROUP_NAME not in text:
        return False, f"persona.md 里没有群名 {brand.DEFAULT_GROUP_NAME}"
    return True, ""


def persona_info() -> dict:
    """人格卡的展示数据：叫什么、多大、最近一次什么时候注的。"""
    p = res("persona.md")
    raw = b""
    try:
        raw = p.read_bytes()
    except OSError:
        pass
    text = raw.decode("utf-8", "replace").lstrip("\ufeff")
    name = "小熊猫"
    tag = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            name = s.lstrip("#").strip() or name
            continue
        if s and not tag and not s.startswith("```"):
            tag = s
            break
    st = load_state()
    last = ""
    for v in st.values():
        if isinstance(v, dict) and v.get("at", "") > last:
            last = v["at"]
    return {"name": name, "tag": tag[:64], "size": len(raw), "last": last}


# ---------------------------------------------------------------- 任务

class Api:
    """前端调用的全部接口。方法名不要以下划线开头，pywebview 会过滤掉。"""

    def __init__(self):
        self._win = None
        self._lock = threading.Lock()
        self._logs: list[dict] = []
        self._seq = 0
        self._done = True
        self._progress = 0
        self._stage = ""
        self._summary = ""
        self._result: dict = {}

    # ---- 内部 ----

    def _log(self, level: str, text: str) -> None:
        with self._lock:
            self._logs.append({"i": self._seq, "level": level, "text": text})
            self._seq += 1
            if len(self._logs) > 400:
                del self._logs[:100]

    def _tick(self, stage: str | None = None, progress: int | None = None) -> None:
        """进度与阶段一起写。

        分两次赋值的话，前端那 220ms 一次的轮询有机会正好夹在中间，读到
        「第 2/4 个」配 25% 这种对不上的组合，进度条看着像在倒退。
        """
        with self._lock:
            if stage is not None:
                self._stage = stage
            if progress is not None:
                self._progress = progress

    def _reset(self) -> None:
        """清空日志并把状态置成「空闲」。accept 条款后走这条路。

        跟 _claim 的区别：_claim 是「抢一个空闲位去干活」（done=False），
        _reset 是「清干净、保持空闲」（done=True）。两者都要在锁里改，
        否则后台线程正在 poll 的时候会读到一半新一半旧的状态。
        """
        with self._lock:
            self._logs.clear()
            self._seq = 0
            self._done = True
            self._progress = 0
            self._stage = "准备中"
            self._summary = ""
            self._result = {}

    def _claim(self) -> bool:
        """抢占执行权。拿到 True 才能起线程。

        必须在一个锁里「检查 + 置位」—— 分成两步的话，两个请求同时进来
        都能看到 _done=True，然后起两个线程同时改同一批文件。写文件这件事
        撞上就是半份内容加半份内容，不是「后写的赢」。
        """
        with self._lock:
            if not self._done:
                return False
            self._logs.clear()
            self._seq = 0
            self._done = False
            self._progress = 0
            self._stage = "准备中"
            self._summary = ""
            self._result = {}
            return True

    # ---- 查询 ----

    def boot(self) -> dict:
        ts = self._probe_all()
        lic_st = license_state()
        # 版本检查走非阻塞：先给缓存 / 空壳，后台线程去打网络。
        # 前端看到 pending=True 就 400ms 后再来问一次。
        up = set_update_state(update_check(nonblock=True))
        # 引流内容同理：先让界面用当前值（内置默认或上次缓存）画出来，
        # 后台线程去拉签名配置。拉到了前端下一次 boot/refresh 就能看到。
        brand_warm()
        pb_ok, pb_why = persona_brand_ok()
        if not pb_ok:
            # 打包的人改了一处忘了另一处。写进日志，界面上也标红。
            self._log("warn", "引流不一致：" + pb_why)
        return {"version": VERSION, "targets": ts,
                "persona": persona_info(), "stats": _stats(ts),
                "consent": consent_state(), "license": lic_st,
                "brand": brand_info(), "update": up,
                "brand_ok": pb_ok, "blocked": hard_block()[0]}

    def rescan(self) -> dict:
        ts = self._probe_all()
        return {"targets": ts, "persona": persona_info(), "stats": _stats(ts),
                "license": license_state(), "brand": brand_info()}

    # ---- 更新 / 引流 ----

    def update_poll(self) -> dict:
        """启动后前端来取后台预热的结果。

        三种状态：
          pending=True  —— 还在查。可能是刚开始，也可能刚拿到缓存的旧值。
                           前端应该继续轮询，别拿这值下结论。
          pending=False —— 有结论了（查到 / 网络失败退缓存）。
        """
        r = update.warm_result()
        if not r:
            return {"pending": True}
        if r.get("pending"):
            # warm() 先塞了缓存占位、后台线程还在跑。这时候返回旧值并
            # 标 pending，前端继续问 —— 以前这里直接置 False，导致前端
            # 拿到几小时前的缓存就停止轮询，后台的真值永远送不出去。
            r["blocked"] = hard_block()[0]
            return r
        set_update_state(r)
        r["blocked"] = hard_block()[0]
        return r

    def update_now(self) -> dict:
        """用户点「检查更新」。跳缓存，一定打网络。"""
        r = set_update_state(update_check(force=True))
        r["pending"] = False
        if r.get("has"):
            self._log("info", f"发现新版 {r.get('ver')}（当前 {VERSION}）")
        elif r.get("err"):
            self._log("dim", f"检查更新失败：{r.get('err')}")
        else:
            self._log("dim", "已是最新版")
        r["blocked"] = hard_block()[0]
        return r

    def brand(self) -> dict:
        """前端补拉引流内容。

        存在的理由：启动时 brand_warm() 在后台线程里跑，首屏画的是内存里
        已有的值（内置默认或上次缓存）。等版本轮询结束，前端调这个接口再
        取一次，就能显示服务端下发的最新内容。

        这里**不做网络请求**：真拉过就返回热的，没拉过就返回当前值 ——
        前端是在启动路径上调的，加一次 4 秒超时的请求等于把白屏搬回来。
        真需要拉的时候由 brand_warm() 或 refresh(force=True) 去做。
        """
        return brand_info()

    def open_url(self, url: str) -> bool:
        """用默认浏览器打开一个链接。只放行 http/https —— 这个接口会从
        前端收到值再交给系统 shell，不限制协议就等于把 os.startfile 暴露
        给页面了（file:// 能打开本地任意文件，配合钓鱼页面能做的事不少）。"""
        u = str(url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        try:
            os.startfile(u)                                    # noqa: S606
            return True
        except OSError:
            return False

    def copy_text(self, text: str) -> dict:
        """把任意文本塞进剪贴板。引流卡「复制群号」用。"""
        return self._clip(str(text or ""))

    def verify_file(self) -> dict:
        """让用户选一个已下载的安装包，跟**已验签**的 SHA-256 比一次。

        为什么这个接口必须存在
        --------------------
        版本信息的 sha256 是签在 wire 里的，客户端也读了、也显示了 —— 但用户
        从浏览器下完文件之后，没有任何一环去核对「下下来的字节是不是签名
        里那一份」。中间人只要在下载环节把 exe 换掉，前面所有的签名校验都
        白做：提示是真的、哈希也是真的，只有文件是假的。

        为什么期望哈希不从参数传
        ----------------------
        前端传什么就信什么，等于把校验做成了装饰。这里从进程内的 `_UPD`
        取，而 `_UPD` 只由 `core.update.check()` 填 —— 那条路径上的 wire
        已经过 Ed25519 验签。前端能改的只有「选哪个文件」。

        比较用 `hmac.compare_digest`：这里是本地文件、攻击面不大，但保持
        全项目一个写法，省得以后有人把这段挪去做别的事时忘了它的性质。
        """
        if not self._win:
            return {"ok": False, "msg": "当前不是窗口模式"}

        with _UPD_LOCK:
            r = dict(_UPD)
        want = str(r.get("sha256") or "").strip().lower()
        if not want:
            return {"ok": False, "msg": "当前版本信息里没有 SHA-256，无法校验"}

        try:
            import webview
            try:
                res = self._win.create_file_dialog(
                    webview.OPEN_DIALOG,
                    file_types=("安装包 (*.exe)", "所有文件 (*.*)"))
            except TypeError:
                # 老版 pywebview 不认 file_types
                res = self._win.create_file_dialog(webview.OPEN_DIALOG)
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False, "msg": f"打开选择框失败：{e}"}
        if not res:
            return {"ok": False, "msg": "取消了"}

        path = res[0] if isinstance(res, (list, tuple)) else str(res)
        p = Path(str(path))
        if not p.is_file():
            return {"ok": False, "msg": "这个路径不是一个文件"}

        # 流式读：安装包十几兆，一次 read() 也能过，但分块写法的内存占用
        # 与文件大小无关，以后换成更大的包不会突然变成内存尖峰。
        h = hashlib.sha256()
        try:
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError as e:
            return {"ok": False, "msg": f"读取失败：{e}"}

        got = h.hexdigest()
        same = hmac.compare_digest(got, want)
        size = p.stat().st_size
        exp_size = int(r.get("size") or 0)
        size_ok = (exp_size == 0) or (size == exp_size)

        if same and size_ok:
            msg = "校验通过，这个文件就是官方发布的安装包"
        elif same and not size_ok:
            # 哈希对但大小不对，理论上不可能（哈希覆盖了全文）。真出现
            # 说明拿到的期望值本身不自洽，报出来而不是硬判通过。
            msg = f"哈希一致但大小不符（{size} vs {exp_size}），请重新下载"
        else:
            msg = "校验不通过：这个文件不是官方发布的安装包，不要运行它"

        return {"ok": bool(same and size_ok), "msg": msg,
                "sha256": got, "want": want,
                "size": size, "want_size": exp_size,
                "name": p.name}

    # ---- 授权（已取消，留接口给旧前端） ----

    def lic_state(self) -> dict:
        return license_state()

    def lic_machine(self) -> dict:
        """只读机器码，不触发网络。现在只用于装机统计展示。"""
        return {"ok": True, "machine": lic.machine_code()}

    def lic_activate(self, code: str) -> dict:
        """已取消。保留返回「不需要激活」，旧前端不会因为拿不到结构而白屏。"""
        return {"ok": True, "machine": lic.machine_code(), "expire": "永久",
                "note": "", "msg": "1.3.0 起无需激活，打开就能用",
                "state": license_state()}

    def lic_heartbeat(self) -> dict:
        """旧前端的「刷新授权」。1.3.0 起没有授权可刷，直接回可用。"""
        return {"ok": True, "state": license_state(), "hint": license_hint(),
                "ticket": _ticket_info(), "msg": "无需授权"}

    def lic_copy(self) -> dict:
        """复制机器码。机器码现在只用于装机统计。"""
        mc = lic.machine_code()
        r = self._clip(mc, "机器码")
        r["machine"] = mc
        return r

    def lic_reset(self) -> dict:
        """旧前端的「清除授权」。现在只是把老版本留下的文件删干净。"""
        licnet.reset_all(DATA_DIR)
        return {"ok": True, "msg": "旧授权记录已清除（1.3.0 不需要授权）",
                "state": license_state()}

    # ---- 剪贴板 ----

    def _clip(self, text: str, what: str = "文本") -> dict:
        """把文本写进系统剪贴板。

        WebView 里 navigator.clipboard 要权限、还要求页面处于焦点，
        无边框窗口经常拿不到 —— 走 Win32 更稳。

        踩过的坑：
          · 64 位下 GlobalAlloc/GlobalLock 必须声明 restype=c_void_p，
            否则句柄被截成 int32，memmove 写到野指针上直接闪退；
          · GlobalAlloc 失败返回 0，不判就往空指针写；
          · SetClipboardData 成功之后那块内存归系统，再 GlobalFree 就是
            双重释放。失败才由调用方释放。
        """
        s = str(text or "")
        if not s:
            return {"ok": False, "text": "", "msg": "没有可复制的内容"}
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.OpenClipboard.argtypes = [ctypes.c_void_p]
            k.GlobalAlloc.restype = ctypes.c_void_p
            k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalLock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.restype = ctypes.c_int
            k.GlobalFree.argtypes = [ctypes.c_void_p]
            u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            u.SetClipboardData.restype = ctypes.c_void_p
            u.CloseClipboard.restype = ctypes.c_int

            if not u.OpenClipboard(None):
                return {"ok": False, "text": s, "msg": "剪贴板被别的程序占用"}
            u.EmptyClipboard()
            size = (len(s) + 1) * 2
            h = k.GlobalAlloc(0x0042, size)      # GMEM_MOVEABLE | GMEM_ZEROINIT
            if not h:
                u.CloseClipboard()
                return {"ok": False, "text": s, "msg": "内存不足，手动抄一下"}
            p = k.GlobalLock(ctypes.c_void_p(h))
            if not p:
                k.GlobalFree(ctypes.c_void_p(h))
                u.CloseClipboard()
                return {"ok": False, "text": s, "msg": "剪贴板内存锁定失败"}
            ctypes.memmove(ctypes.c_void_p(p), ctypes.c_wchar_p(s), size)
            k.GlobalUnlock(ctypes.c_void_p(h))
            if not u.SetClipboardData(13, ctypes.c_void_p(h)):   # CF_UNICODETEXT
                k.GlobalFree(ctypes.c_void_p(h))
                u.CloseClipboard()
                return {"ok": False, "text": s, "msg": "写入剪贴板失败"}
            u.CloseClipboard()
            return {"ok": True, "text": s, "msg": f"{what}已复制"}
        except Exception:                                      # noqa: BLE001
            return {"ok": False, "text": s, "msg": "复制失败，手动抄一下"}

    # ---- 使用前须知 ----

    def agree(self) -> dict:
        """用户点了「同意并继续」。记一笔，之后不再拦。"""
        rec = save_consent()
        self._reset()          # _reset 已经把 _done 置回 True（空闲），不用再手动设
        self._log("ok", "已接受使用条款 · " + rec["at"])
        return {"ok": True, "at": rec["at"], "version": CONSENT_VERSION}

    def disagree(self) -> dict:
        """不同意：直接退出。没有任何功能可用。"""
        threading.Timer(0.15, lambda: os._exit(0)).start()
        return {"ok": True, "msg": "已退出"} 

    @staticmethod
    def _probe_all() -> list[dict]:
        live = proc.processes()        # 只枚举一次，4 个目标共用
        return [probe(t, live) for t in all_targets()]

    def poll(self) -> dict:
        """前端 220ms 拉一次。整块在锁里读 —— 后台线程正在改这些字段，
        不加锁会拿到「旧进度 + 新 done」这种自相矛盾的组合。
        """
        with self._lock:
            logs = list(self._logs)
            return {"logs": logs, "progress": self._progress,
                    "stage": self._stage, "done": self._done,
                    "summary": self._summary, "result": dict(self._result)}

    # ---- 执行 ----

    def apply(self, names=None) -> dict:
        return self._start("apply", names)

    def revert(self, names=None) -> dict:
        return self._start("revert", names)

    def _start(self, mode: str, names) -> dict:
        # 双保险：前端在未同意时根本不会显示操作按钮，但接口层也堵一次 ——
        # 前端可以被绕过，写文件这件事不能只靠界面把关。
        # 带上 need_consent：前端靠这个字段决定拉起条款面板，而不是坐在
        # 那儿等一个永远不会来的任务（_claim 都没走到，poll 永远是空闲）。
        if not consent_state()["accepted"]:
            return {"ok": False, "msg": "请先阅读并接受使用条款",
                    "need_consent": True}
        # 授权闸门放在条款之后：先讲清楚规则，再讲授权。
        # 1.3.0 起 license_ok() 恒定 True，留着是因为还有别的调用点，
        # 一次性删签名回归面太大。
        if not license_ok(force=True):
            return {"ok": False, "msg": license_hint(), "need_license": True}
        # 强制更新：后台标记了 force，旧版就不许再动文件。放最后 —— 条款
        # 和授权是这个产品自己的规则，更新是运维手段，顺序别搞反。
        blk, why = hard_block()
        if blk:
            return {"ok": False, "msg": why, "need_update": True}
        if isinstance(names, str):
            names = [names]
        # 放在授权之后：被拒的请求不该白占执行位
        if not self._claim():
            return {"ok": False, "msg": "已有任务在执行"}
        self._log("dim", f"开始{('破甲' if mode == 'apply' else '还原')}")
        threading.Thread(target=self._run, args=(mode, names), daemon=True).start()
        return {"ok": True}

    def _run(self, mode: str, names) -> None:
        try:
            targets = resolve(names)
            if not targets:
                self._log("bad", "没有匹配的目标")
                # 跟其他分支一样在锁内落地，别让 poll() 读到「done=False +
                # 新 summary + 旧 result」这种拼不上的组合
                with self._lock:
                    self._summary = "没有匹配的目标"
                    self._result = {"ok": 0, "total": 0,
                                    "partial": False, "failed": True}
                return
            persona = ""
            if mode == "apply":
                persona = load_persona()
                self._log("dim", f"人格文件 {len(persona):,} 字符")
            total = len(targets)
            ok = 0
            total_changed = 0        # 实际改动了多少个文件，还原时用来说实话
            live = proc.processes()
            restart: list[str] = []
            for i, t in enumerate(targets):
                label = LABELS.get(t.name, t.name)
                self._tick(stage=f"{i + 1}/{total}")
                self._log("info", f"—— {label}")
                try:
                    rep = t.inject(persona) if mode == "apply" else t.revert()
                except Exception as e:                         # noqa: BLE001
                    self._log("bad", f"{label}: 异常 {e}")
                    self._log("dim", traceback.format_exc(limit=2).strip())
                    self._tick(progress=int((i + 1) / total * 100))
                    continue
                lvl = "ok" if rep.ok else "bad"
                if not rep.found:
                    lvl = "dim"
                self._log(lvl, rep.line())
                for s in rep.skipped[:4]:
                    self._log("dim", "   · " + s)

                # 写完了要读回来确认。日志说「已改」不算数，磁盘上真有块才算。
                if mode == "apply" and rep.found:
                    after = probe(t, live)
                    if after["state"] not in ("injected", "partial"):
                        self._log("bad", f"  自检未通过：{after['detail']}")
                        rep.ok = False
                    else:
                        self._log("dim", f"  自检通过 · {after['injected']}/{after['total']} 个靶点")
                        # 只有这次真写成功的才提示重启。写失败的也塞进
                        # 「关掉重开生效」，用户重开客户端会发现人格没变，
                        # 白折腾一轮。
                        if after.get("running"):
                            restart.append(label)

                if rep.ok:
                    ok += 1
                total_changed += len(rep.changed)
                self._tick(progress=int((i + 1) / total * 100))

            verb = "破甲" if mode == "apply" else "还原"
            if ok == total:
                summary = f"{verb}完成 · {ok}/{total} 个目标"
            elif ok:
                summary = f"{verb}完成 {ok}/{total}，其余看日志"
            else:
                summary = f"{verb}失败 · 0/{total}"
            # 还原要多说一句改了哪些文件。只报「4/4 个目标」太像敷衍 ——
            # 用户上一版就是被这句骗过：目标全「成功」，磁盘一个字没变。
            if mode != "apply" and ok == total and not total_changed:
                summary = "没有需要还原的注入"
            elif mode != "apply" and total_changed:
                summary += f"｜{total_changed} 个文件"
            self._log("warn" if ok < total else "ok", summary)
            if mode == "apply" and ok:
                if restart:
                    self._log("warn", "关掉 " + "、".join(restart) + " 再打开就生效")
                    summary += "｜关掉重开生效"
                else:
                    self._log("dim", "下次打开客户端时生效")
            # 结构化结果和 summary 一起落，中间不给前端读到「done=True 但
            # result 还是空的」的机会。界面别去猜 summary 里有没有「失败」
            # 两个字 —— 「完成 2/4，其余看日志」不含它，但确实没全成功。
            with self._lock:
                self._summary = summary
                self._result = {"ok": ok, "total": total,
                                "partial": 0 < ok < total}
        except SystemExit as e:
            self._log("bad", str(e))
            with self._lock:
                self._summary = "找不到人格文件"
                self._result = {"ok": 0, "total": 0, "partial": False, "failed": True}
        except Exception:                                      # noqa: BLE001
            err = traceback.format_exc()
            self._log("bad", err.strip().splitlines()[-1])
            self._log("dim", "\n".join(err.strip().splitlines()[-6:]))
            with self._lock:
                self._summary = "执行异常，看日志"
                self._result = {"ok": 0, "total": 0, "partial": False, "failed": True}
            crash_log(err)
        finally:
            with self._lock:
                # stage 已经置成「完成」，进度条不能停在半路 —— 中途抛异常时
                # _progress 可能是 50，界面就会永远显示一条走了一半的条，
                # 用户以为还在跑。失败也是结束，跑满。
                # total=0（没匹配到目标）时保持 0，那种情况不该显示 100%。
                if self._result.get("total"):
                    self._progress = 100
                self._stage = "完成"
                self._done = True

    # ---- 窗口 / 系统 ----

    def win_min(self) -> bool:
        try:
            if self._win:
                self._win.minimize()
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def win_close(self) -> bool:
        # 写到一半被关掉会留下半份文件，宁可让用户多等几秒。
        # 读 _done 走锁 —— 后台线程刚好在 finally 里写它，不加锁读到什么
        # 全看运气（虽然 CPython 里 bool 赋值是原子的，但这属于实现细节，
        # 不该拿它当并发保证）。
        with self._lock:
            busy = not self._done
        if busy:
            return False
        try:
            if self._win:
                self._win.destroy()
            return True
        except Exception:                                      # noqa: BLE001
            os._exit(0)

    def drag(self) -> bool:
        """标题栏拖动交给系统：ReleaseCapture + WM_NCLBUTTONDOWN / HTCAPTION。

        SendMessage 会一直阻塞到松手，所以丢到后台线程，前端立即拿到结果。
        """
        hwnd = _find_hwnd(WIN_TITLE)
        if not hwnd:
            return False
        try:
            u = ctypes.windll.user32
            u.ReleaseCapture()

            def go():
                try:
                    u.SendMessageW(hwnd, 0x00A1, 2, 0)
                except Exception:                              # noqa: BLE001
                    pass
            threading.Thread(target=go, daemon=True).start()
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def win_pos(self) -> dict:
        """当前窗口左上角坐标。前端拖动时拿它当基准。"""
        h = _window_handle()
        if not h:
            return {"ok": False, "x": 0, "y": 0}
        r = _rect(h)
        return {"ok": True, "x": r[0], "y": r[1]}

    def win_move(self, x: int, y: int) -> bool:
        """把窗口挪到屏幕绝对坐标。

        SetWindowPos 是唯一在无边框窗口上稳定生效的移动方式 ——
        SendMessage(WM_NCLBUTTONDOWN, HTCAPTION) 那条路要求窗口有可拖标题栏，
        我们早把 WS_CAPTION 去掉了，发过去系统根本不进拖动循环。
        """
        h = _window_handle()
        if not h:
            return False
        try:
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(h), None, int(x), int(y), 0, 0,
                0x0001 | 0x0004 | 0x0010)      # NOSIZE | NOZORDER | NOACTIVATE
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def move_by(self, dx: int, dy: int) -> bool:
        """按增量挪窗口（旧接口，保留给老前端）。"""
        pos = self.win_pos()
        if not pos.get("ok"):
            return False
        return self.win_move(pos["x"] + int(dx), pos["y"] + int(dy))

    # ---- 体检 ----

    def verify(self, names=None) -> dict:
        """只读检查。不动磁盘，随时可以点。

        体检也要过条款：它会读客户端目录结构、报告版本号，等于一次探测。
        强制更新期间也拦 —— 旧版给出的检查结论已经不可信。
        """
        if not consent_state()["accepted"]:
            return {"targets": [], "msg": "请先接受使用条款", "need_consent": True}
        if not license_ok(force=True):
            return {"targets": [], "msg": license_hint(), "need_license": True}
        blk, why = hard_block()
        if blk:
            return {"targets": [], "msg": why, "need_update": True}
        persona = ""
        try:
            persona = load_persona()
        except SystemExit:
            pass
        report = []
        for t in resolve(names):
            try:
                items = t.verify(persona)
            except Exception as e:                             # noqa: BLE001
                items = [{"ok": False, "text": f"检查出错：{e}", "hint": ""}]
            absent = len(items) == 1 and items[0]["text"] == "未安装"
            bad = sum(1 for i in items if not i["ok"])
            report.append({
                "name": t.name,
                "label": LABELS.get(t.name, t.name),
                "ok": bad == 0,
                "absent": absent,
                "items": items,
            })
        return {"targets": report}

    # ---- 手动指定目录 ----

    def pick_dir(self, name: str) -> dict:
        """弹系统文件夹选择框。自动探测扫不到时用这个兜底。

        选完先校验目录像不像——用户手动选目录时最常见的错是选到安装
        目录、或者选到某个子目录。那种情况下如果照单全收，界面会显示
        「已安装」，点破甲却一个靶点都写不进去，用户根本不知道错在哪。
        """
        if name not in ("codex", "wb", "zcode", "dsh"):
            return {"ok": False, "msg": "未知客户端"}
        if not self._win:
            return {"ok": False, "msg": "当前不是窗口模式"}
        cls = REGISTRY.get(name)
        try:
            import webview
            # 起始目录给一个「大概率该选这里」的位置，省得用户从「文档」翻起
            res = self._win.create_file_dialog(webview.FOLDER_DIALOG,
                                               directory=_suggest_dir(name))
        except TypeError:
            # 老版 pywebview 不认 directory 参数
            try:
                res = self._win.create_file_dialog(webview.FOLDER_DIALOG)
            except Exception as e:                             # noqa: BLE001
                return {"ok": False, "msg": f"打开选择框失败：{e}"}
        except Exception as e:                                 # noqa: BLE001
            return {"ok": False, "msg": f"打开选择框失败：{e}"}
        if not res:
            return {"ok": False, "msg": "取消了"}

        path = res[0] if isinstance(res, (list, tuple)) else str(res)
        p = Path(str(path))
        if not p.is_dir():
            return {"ok": False, "msg": "这个路径不是一个文件夹"}
        if cls is not None:
            try:
                ok, why = cls.looks_like(p)
            except Exception:                                  # noqa: BLE001
                ok, why = True, ""       # 校验函数自己出错，不拦用户
            if not ok:
                return {"ok": False, "msg": why or "这个目录不像对应的客户端目录"}
        if not save_root(name, str(p)):
            return {"ok": False, "msg": "保存失败"}
        return {"ok": True, "path": str(p)}

    def forget_dir(self, name: str) -> dict:
        """清掉手动指定的目录，回到自动探测。

        跟 save_root 用同一个文件、同一种原子写 —— 直接 write_text 覆盖
        在「用户刚选完目录就点忘记」这种连点下会把整个 paths.json 写成
        半截，下次 raw_root() 读不出来，所有手动目录一起失效。
        """
        if name not in ("codex", "wb", "zcode", "dsh"):
            return {"ok": False, "msg": "未知客户端"}
        try:
            d = json.loads(core_base.PATHS_FILE.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            return {"ok": True, "targets": self._probe_all()}
        if name not in d:
            return {"ok": True, "targets": self._probe_all()}
        d.pop(name, None)
        try:
            core_base.atomic_write(core_base.PATHS_FILE,
                                   json.dumps(d, ensure_ascii=False, indent=2))
        except OSError as e:
            return {"ok": False, "msg": f"写入失败：{e}"}
        return {"ok": True, "targets": self._probe_all()}

    # ---- 守护 ----

    def watch_state(self) -> dict:
        last = ""
        try:
            p = DATA_DIR / "watch.log"
            if p.exists():
                lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines:
                    last = lines[-1][:19]
        except OSError:
            pass
        return {"on": autostart_on(), "cmd": _self_cmd(), "last": last}

    def watch_toggle(self, on: bool) -> dict:
        on = bool(on)
        if on and not consent_state()["accepted"]:
            return {"ok": False, "on": autostart_on(), "msg": "请先接受使用条款"}
        if on and not license_ok(force=True):
            return {"ok": False, "on": autostart_on(), "msg": license_hint(),
                    "need_license": True}
        if on and hard_block()[0]:
            return {"ok": False, "on": autostart_on(),
                    "msg": hard_block()[1], "need_update": True}
        if not autostart_set(on):
            return {"ok": False, "on": autostart_on(), "msg": "改注册表失败"}
        if on:
            # 立刻拉起一个，不用等下次开机
            try:
                import subprocess
                flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(_watch_argv(), creationflags=flags,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:                         # noqa: BLE001
                return {"ok": False, "on": True, "msg": f"自启已开，但拉起失败：{e}"}
        return {"ok": True, "on": on, "msg": "守护已开，插件更新后会自动补" if on else "守护已关"}

    # ---- 备份 ----

    def backups(self) -> dict:
        items = list_backups()
        return {"items": items,
                "total": len(items),
                "size": sum(int(i.get("size") or 0) for i in items)}

    def prune_backups(self, keep: int = 10) -> dict:
        n, freed = prune_backups(int(keep) if keep else 10)
        return {"ok": True, "removed": n, "freed": freed,
                "text": f"清掉 {n} 份旧备份，释放 {freed // 1024} KB" if n else "没有可清理的旧备份"}

    def restore_point(self, bid: str) -> dict:
        """回滚到某次注入之前。跟 revert 不同：revert 只回最近一次。

        这是写操作 —— 它把备份内容覆盖回客户端文件，跟 apply 一个量级。
        所以闸门跟 apply 一样三道：条款 + 授权 + 强制更新。
        """
        if not consent_state()["accepted"]:
            return {"ok": False, "files": 0, "errors": [],
                    "text": "请先阅读并接受使用条款", "need_consent": True}
        if not license_ok(force=True):
            return {"ok": False, "files": 0, "errors": [],
                    "text": license_hint(), "need_license": True}
        if hard_block()[0]:
            return {"ok": False, "files": 0, "errors": [],
                    "text": hard_block()[1], "need_update": True}
        if not bid:
            return {"ok": False, "files": 0, "errors": [], "text": "没指定要回滚哪一次"}
        n, bad = restore_backup(bid)
        if bad:
            txt = f"回滚 {bid}：{n} 个文件好了，{len(bad)} 个失败"
        else:
            txt = f"回滚 {bid}：{n} 个文件已还原"
        return {"ok": not bad, "files": n, "errors": bad, "text": txt}

    def open_data_dir(self) -> bool:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(DATA_DIR))                        # noqa: S606
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def open_path(self, p: str) -> bool:
        try:
            q = Path(p)
            if q.is_file():
                q = q.parent
            os.startfile(str(q))                               # noqa: S606
            return True
        except Exception:                                      # noqa: BLE001
            return False


def _stats(targets: list[dict]) -> dict:
    """人格卡右侧那几个数字。"""
    return {
        "installed": sum(1 for t in targets if t["installed"]),
        "injected": sum(1 for t in targets if t["state"] == "injected"),
        "todo": sum(1 for t in targets if t["injectable"] and t["state"] != "injected"),
        "files": sum(t["injected"] for t in targets),
        "targets": sum(t["total"] for t in targets),
        "running": sum(1 for t in targets if t.get("running")),
    }


_MUTEX = None


def take_single_instance() -> bool:
    """同一时间只允许一个窗口。

    双击两次会开两个，两边同时写同一批文件，备份也互相覆盖。
    已有一个在跑就把它叫到前面，自己安静退出。
    """
    global _MUTEX
    try:
        k = ctypes.windll.kernel32
        k.OpenMutexW.restype = ctypes.c_void_p
        k.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]

        name = "Local\\XiaoXiongMaoPoJia"
        probe = k.OpenMutexW(0x00100000, False, name)      # SYNCHRONIZE
        if probe:
            k.CloseHandle(ctypes.c_void_p(probe))
            h = _find_hwnd(WIN_TITLE)
            if h:
                u = ctypes.windll.user32
                u.ShowWindow(ctypes.c_void_p(h), 9)        # SW_RESTORE
                u.SetForegroundWindow(ctypes.c_void_p(h))
            return False
        _MUTEX = k.CreateMutexW(None, False, name)         # 持有到进程退出
        return True
    except Exception:                                      # noqa: BLE001
        return True                                        # 拿不到锁就当没锁，别挡住用户


def _find_hwnd(title: str) -> int:
    """按标题找窗口句柄。标题被改过就退回枚举匹配。"""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        h = u.FindWindowW(None, title)
        if h:
            return int(h)
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _lp):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if title in buf.value and u.IsWindowVisible(hwnd):
                    found.append(int(hwnd))
            return True

        u.EnumWindows(cb, 0)
        return found[0] if found else 0
    except Exception:                                          # noqa: BLE001
        return 0


# ---------------------------------------------------------------- 后台守护

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "XiaoXiongMaoWatch"


def _self_cmd() -> str:
    """自启命令行。打包后是 exe 自己，源码模式要带脚本路径。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --watch'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --watch'


def _watch_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--watch"]
    return [sys.executable, str(Path(__file__).resolve()), "--watch"]


def autostart_on() -> bool:
    """注册表里有没有自启项，且它指向的 exe 还在。

    只看「键存在」是不够的：用户把 exe 挪了、或者从下载目录直接运行过
    一次就点了开守护，注册表项会指向一个已经不存在的路径。那种情况下
    界面显示「守护 开」而实际从来没跑起来，用户以为有人盯着，其实没有。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
        if not v:
            return False
        raw = str(v).strip()
        # 命令行形如 "C:\path\app.exe" --watch，取第一段引号里的路径
        target = raw[1:raw.index('"', 1)] if raw.startswith('"') else raw.split(" ")[0]
        return Path(target).exists()
    except (OSError, ValueError):
        return False


def autostart_set(on: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _self_cmd())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def watch_loop(interval: int = 60) -> int:
    """守护：只在「以前注入过、现在丢了」时补一刀。

    典型场景是客户端升级插件 —— 新建了一个干净的版本目录，旧注入没了。
    从没碰过的客户端不动：那不属于本工具的职责。
    """
    log_path = DATA_DIR / "watch.log"

    def w(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    w(f"守护启动 pid={os.getpid()} 间隔={interval}s")
    try:
        persona = load_persona()
    except SystemExit as e:
        w(f"人格文件读不到：{e}")
        return 1

    #: 上次真打版本接口的时间。守护是常驻进程，每分钟都问一次会把服务端
    #: 打成筛子，所以按 30 分钟一轮 —— 后台发了强制更新，最长半小时后
    #: 守护自己停下来。这个延迟是刻意的：守护不拦用户，它只是「别再往
    #: 旧版上补文件了」。
    ver_at = 0.0
    paused = False

    while True:
        try:
            # 授权那层 1.3.0 已经拆了，license_ok() 恒 True。这里保留调用
            # 是给以后留插桩位 —— 真要做「按账号封禁」，改那一个函数。
            if not license_ok():
                w(f"授权失效，守护暂停：{license_hint()}")
                time.sleep(max(60, interval * 5))
                continue

            # 强制更新检查。守护进程跟 GUI 走同一套判断：只有服务端明确
            # 说 has+force 才停。网络失败放行 —— 服务端挂了不能让用户的
            # 守护静默死掉，那种「明明开着却没生效」比「多补一次」更难查。
            now = time.time()
            if now - ver_at >= 1800:
                ver_at = now
                try:
                    r = update.check(DATA_DIR, VERSION, force=True)
                except Exception as e:                         # noqa: BLE001
                    r = {"has": False, "err": str(e)}
                blocked = bool(r.get("has") and r.get("force"))
                if blocked and not paused:
                    w(f"后台要求强制更新到 {r.get('ver')}，守护暂停补刀")
                    w(f"下载地址 {r.get('url') or '(未填)'}")
                elif not blocked and paused:
                    w("强制更新标记已撤，守护恢复")
                paused = blocked
            if paused:
                time.sleep(max(60, interval * 5))
                continue

            st = load_state()
            touched = False
            for t in all_targets():
                if not st.get(t.name, {}).get("at"):
                    continue                  # 从没注入过，不主动碰
                if not t.detect():
                    continue
                hits = t.collect()
                if not hits:
                    continue
                done = sum(1 for h in hits if t.is_applied(h))
                if done >= len(hits):
                    continue
                w(f"{t.name}: {done}/{len(hits)} 个靶点，自动补")
                rep = t.inject(persona)
                w(f"{t.name}: {rep.line()}")
                touched = True
            # 守护每次补刀都会新落一份备份。客户端升级是常态（WorkBuddy
            # 插件缓存按版本目录滚动），跑上几个月能攒出几千个目录，
            # 备份列表打开要转半天。自动只留最近 30 份 —— 用户手动清理
            # 那档（10 份）更狠，这里留宽一点，别把人家可能要用到的删了。
            if touched:
                try:
                    n, _ = prune_backups(30)
                    if n:
                        w(f"自动清理旧备份 {n} 份")
                except Exception as e:                     # noqa: BLE001
                    w(f"清理备份出错：{e}")
        except Exception as e:                             # noqa: BLE001
            w(f"检查出错：{e}")
        time.sleep(max(15, interval))


# ---------------------------------------------------------------- WebView2 壳

class Borderless:
    """把窗口做成无边框深色。WebView2 的窗口是 WinForms Form，
    用 SetWindowLong 去掉 WS_CAPTION，再按 Win11 规则给圆角。"""

    def __init__(self, hwnd: int):
        self.h = hwnd

    def apply(self) -> None:
        try:
            u = ctypes.windll.user32
            gwl_style = -16
            style = u.GetWindowLongW(self.h, gwl_style)
            # 去掉标题栏和边框，保留 sysmenu / minbox / thickframe 以便贴边
            style &= ~0x00C00000          # WS_CAPTION
            u.SetWindowLongW(self.h, gwl_style, style)
            # Win11 圆角
            try:
                dwm = ctypes.windll.dwmapi
                pref = ctypes.c_int(2)    # DWMWCP_ROUND
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(self.h), 33,
                                          ctypes.byref(pref), ctypes.sizeof(pref))
                dark = ctypes.c_int(1)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(self.h), 20,
                                          ctypes.byref(dark), ctypes.sizeof(dark))
            except Exception:                                  # noqa: BLE001
                # Win10 没有 DWMWA_WINDOW_CORNER_PREFERENCE，调用会失败。
                # 圆角是装饰，没有就是方角，不影响任何功能。
                pass
            u.SetWindowPos(ctypes.c_void_p(self.h), None, 0, 0, 0, 0,
                           0x0002 | 0x0001 | 0x0004 | 0x0020)   # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
        except Exception:                                      # noqa: BLE001
            # 整段无边框改造失败 = 退回系统默认的带标题栏窗口。界面照常能用，
            # 只是视觉上不是设计稿那样。抛出去会让窗口直接打不开，不能这么做。
            pass


def _fit(hwnd: int, w: int, h: int) -> None:
    """无边框后窗口尺寸 = 客户区尺寸，重新按设计稿尺寸摆一次。

    pywebview 的 width/height 算的是含标题栏的整窗，去掉 WS_CAPTION 之后
    客户区会矮掉 40~60 px，底栏会被裁 —— 这里按客户区重设。
    """
    try:
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(hwnd), None, 0, 0, int(w), int(h), 0x0002 | 0x0004)
    except Exception:                                          # noqa: BLE001
        # 窗口尺寸没调准最差是底栏被裁几十像素，不影响任何功能。
        # 这里绝不抛：_fit 在各种时机被调（启动、DPI 变化、拖动后），
        # 抛出去会在 WebView 的事件回调里变成一堆无意义的崩溃日志。
        pass


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_WH = 0


def _window_handle() -> int:
    """窗口句柄。第一次找到就缓存 —— 拖动时每帧都要用，别每次都 FindWindow。"""
    global _WH
    if _WH:
        return _WH
    _WH = _find_hwnd(WIN_TITLE)
    return _WH


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _RECT()
    try:
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    except Exception:                                          # noqa: BLE001
        return 0, 0, 0, 0
    return r.left, r.top, r.right, r.bottom


def _center(hwnd: int) -> None:
    """把窗口挪到主屏中央（偏上一点，视觉重心更稳）。"""
    try:
        u = ctypes.windll.user32
        r = ctypes.create_string_buffer(16)          # RECT: 4 × LONG
        u.GetWindowRect(ctypes.c_void_p(hwnd), r)
        left, top, right, bottom = (int.from_bytes(r.raw[i:i + 4], "little", signed=True)
                                    for i in (0, 4, 8, 12))
        w, h = right - left, bottom - top
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 24)
        u.SetWindowPos(ctypes.c_void_p(hwnd), None, x, y, 0, 0, 0x0001 | 0x0004)
    except Exception:                                          # noqa: BLE001
        # 居中失败 = 窗口出现在系统默认位置，功能不受影响。宁可位置不理想
        # 也不能在这里抛 —— 它在 shown 事件里，抛出去用户看到的是窗口闪退。
        pass


def run_webview(html: str) -> int:
    import webview

    api = Api()
    win = webview.create_window(
        WIN_TITLE, html=html, js_api=api,
        width=1060, height=740, min_size=(880, 620),
        background_color="#0b0810", resizable=True,
        frameless=True, easy_drag=False,
    )
    api._win = win

    def on_shown():
        time.sleep(0.35)
        h = _find_hwnd(WIN_TITLE)
        if h:
            Borderless(h).apply()
            _fit(h, 1060, 740)
            _center(h)

    try:
        win.events.shown += on_shown
    except Exception:                                          # noqa: BLE001
        # 挂不上 shown 事件 = 无边框和居中不生效，窗口还是正常的带边框窗口。
        # 某些 pywebview / WebView2 版本不给 events.shown，这属于降级不是错误。
        pass

    webview.start(debug=bool(os.environ.get("XM_DEBUG")), gui="edgechromium")
    return 0


# ---------------------------------------------------------------- 浏览器回退

def run_browser(html: str) -> int:
    """没有 WebView2 时，用本地 HTTP + Edge --app 起同一个界面。"""
    import http.server
    import socketserver
    import urllib.parse
    import webbrowser

    api = Api()

    def call(name, args):
        fn = getattr(api, name, None)
        if not callable(fn):
            return {"error": "no such method"}
        return fn(*args)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):                             # noqa: A003
            pass

        def _local_only(self) -> bool:
            """挡住别的网页偷偷调接口。

            回退模式起的是一个本地 HTTP 服务，页面靠 fetch 调 /api/<方法>。
            任何本机浏览器里打开的网页都能对着 127.0.0.1:<port> 发 POST ——
            浏览器同源策略不拦「跨站写」，只拦「跨站读」，所以攻击页面读不到
            返回体，但请求照样打进来，等于让它替你点了「破甲」或「还原」。
            两道卡：Origin 必须是本服务自己（或没有 Origin 的非浏览器客户端），
            Host 必须是 127.0.0.1/localhost，挡掉 DNS rebinding。
            """
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return False
            origin = self.headers.get("Origin")
            if origin and f"127.0.0.1:{port}" not in origin \
                    and f"localhost:{port}" not in origin:
                return False
            return True

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                      # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if not self._local_only():
                self._send(403, b"forbidden", "text/plain")
                return
            if path in ("/", "/index.html"):
                self._send(200, html.encode("utf-8"), "text/html")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):                                     # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if not self._local_only():
                self._send(403, b"forbidden", "text/plain")
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"[]"
            try:
                args = json.loads(raw.decode("utf-8") or "[]")
                if not isinstance(args, list):
                    args = [args]
            except ValueError:
                args = []
            name = path.rsplit("/", 1)[-1]
            try:
                body = json.dumps(call(name, args), ensure_ascii=False).encode("utf-8")
            except Exception as e:                             # noqa: BLE001
                body = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            self._send(200, body)

    class Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Srv(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    edge = _find_edge()
    if edge:
        import subprocess
        subprocess.Popen([edge, f"--app={url}",                       # noqa: S603
                          "--window-size=1060,740", "--no-first-run"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


def _find_edge() -> str:
    for p in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(p).exists():
            return p
    return ""


def gui() -> int:
    if not take_single_instance():
        return 0
    html = load_html()
    try:
        return run_webview(html)
    except Exception as e:                                     # noqa: BLE001
        crash_log(traceback.format_exc())
        print(f"[warn] WebView2 不可用（{e}），改用浏览器窗口", file=sys.stderr)
        return run_browser(html)


# ---------------------------------------------------------------- CLI

def require_consent(argv: list[str]) -> bool:
    """命令行入口的同意闸门。

    GUI 那边是弹面板，命令行这边直接问一句 y/N —— 不接受就返回 False，
    调用方以非 0 退出。`--accept` 可以免交互（装机脚本 / 批处理用），但
    仍然会把同意记录落到 consent.json，跟点按钮的效果一样。
    """
    if consent_state()["accepted"]:
        return True
    if "--accept" in argv:
        save_consent()
        print("已记录：接受使用条款")
        return True
    print(disclaimer_text())
    # stdin 可能是 None：windowed 版 exe 双击启动时没有控制台，PyInstaller
    # 会把 sys.stdin 置成 None，input() 直接抛 RuntimeError（不是 EOFError，
    # 下面那个 except 接不住）。远程 / 计划任务里跑也一样。
    # 这种时候不能崩，也不能默默当成「已同意」—— 返回 False 让调用方以
    # 非 0 退出，并在 stderr 上说清楚该加的参数。
    if sys.stdin is None:
        print("当前没有可交互的终端。要免交互接受条款，重跑时加 --accept。",
              file=sys.stderr)
        return False
    try:
        ans = input("输入 y 表示已阅读并接受以上全部条款，其它任意输入即退出：")
    except (EOFError, KeyboardInterrupt):
        return False
    except (RuntimeError, OSError):
        # 控制台被关掉 / 句柄失效，同样按「没同意」处理
        print("读取输入失败。要免交互接受条款，重跑时加 --accept。",
              file=sys.stderr)
        return False
    if ans.strip().lower() not in ("y", "yes"):
        print("未接受条款，已退出。")
        return False
    save_consent()
    print("已记录：接受使用条款\n")
    return True


def cmd_status() -> int:
    print(f"{APP_NAME} · 本机客户端")
    for t in all_targets():
        label = LABELS.get(t.name, t.name)
        print(f"  {label:<12} {t.status()}")
        # 手动目录有问题时补一行。只写「未安装」的话，用户明明指定过目录，
        # 会以为这个工具不认，转头去重装客户端。
        note = getattr(t, "manual_note", "") or ""
        raw = core_base.raw_root(t.name)
        if raw and note:
            print(f"  {'':<12} 手动目录 {raw}")
            print(f"  {'':<12}   {note}")
        elif raw:
            print(f"  {'':<12} 手动目录 {raw}")
    print(f"\n版本 {VERSION} · 交流群 {brand.GROUP_NO}")
    return 0


def cmd_update(apply_state: bool = True) -> int:
    """查一次版本。CLI 入口在 require_license() 之前调它，让闸门有依据。"""
    r = update_check(force=True)
    if apply_state:
        set_update_state(r)
    print(f"{APP_NAME} · 版本 {VERSION}")
    if r.get("err"):
        print(f"  检查失败  {r['err']}")
        return 0                      # 网络问题不算失败，更不能拦人
    if not r.get("has"):
        print("  已是最新版")
        return 0
    print(f"  发现新版  {r.get('ver')}")
    print(f"  强制更新  {'是' if r.get('force') else '否'}")
    if r.get("url"):
        print(f"  下载地址  {r['url']}")
    if r.get("sha256"):
        print(f"  SHA-256   {r['sha256']}")
    if r.get("notes"):
        print("  更新说明")
        for line in str(r["notes"]).splitlines():
            print(f"    {line}")
    print(f"\n下载后覆盖原 exe 即可（{brand.LINE}）")
    return 0


def _sha256_file(p: Path) -> tuple[str, int, str]:
    """流式算 SHA-256。返回 (hex, 字节数, 错误)。"""
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest(), p.stat().st_size, ""
    except OSError as e:
        return "", 0, str(e)


def cmd_verify_download(paths) -> int:
    """核对已下载的安装包。

        xm verify-download                   直接用签名哈希
        xm verify-download D:\\a.exe D:\\b.exe  一次核多个

    哈希来源必须是**这次联网拿到的、已验签的**版本信息，不能只读本地
    缓存。理由：cache 是磁盘上的普通 JSON，谁都能改。如果只信缓存，
    攻击者改掉 `~/.xiaoxiongmao/update.json` 里的 sha256，这个命令就会
    把他换过的 exe 判成「官方发布」—— 校验标准被换掉，校验就没意义了。

    拿不到可信哈希时（断网且无缓存、服务端没签名）一律判不通过并说明
    原因，绝不退化成「跳过校验、当作通过」。
    """
    r = update_check(force=True)
    tamper = str(r.get("tamper") or "")
    want = str(r.get("sha256") or "").strip().lower()
    exp_size = int(r.get("size") or 0)
    # src 由 core/update.py 标：remote = 本次联网且验签过；
    # cache = 退回缓存但**重新验签过**。两者可信度一样，只是要让用户
    # 知道手里的结论是「刚问到的」还是「上次的」。
    src = "本地缓存（已重新验签）" if r.get("src") == "cache" else "本次联网"

    print(f"{APP_NAME} · 安装包核验")
    if tamper:
        # check() 在验签失败时会把结果清成空结构，want 一定是空的。
        # 单独报，因为「有人在改版本信息」和「就是没填哈希」是两回事。
        print(f"  无法核验：版本信息校验失败（{tamper}）")
        print("  这条消息本身可能被篡改过，拿不到可信哈希就不能判通过。")
        return 1
    if r.get("err") and r.get("src") == "cache":
        # 缓存可用但网络不通：能核，先说清楚这是离线结论。
        print(f"  [注意] 连不上服务端（{r['err']}）")
    if r.get("err") and not want:
        print(f"  无法核验：{r['err']}")
        print("  没有可信哈希时不能凭文件名或大小放行。")
        return 1
    if not want:
        print("  无法核验：服务端这次发布的版本信息里没有 SHA-256")
        print("  让发布者在后台上传时填上，否则谁也没法自证拿到的包是官方的。")
        return 1

    print(f"  哈希来源  {src}")
    print(f"  签名版本  {r.get('ver') or '(未知)'}")
    print(f"  期望哈希  {want}")
    if exp_size:
        print(f"  期望大小  {exp_size} B")

    files = [Path(x) for x in (paths or []) if str(x).strip()]
    if not files:
        # 没给路径就核当前 exe 自己。这是最常被问的一句话：「我这版是不是
        # 官方的」—— 双击就能查，不用先去找安装包。
        exe = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
        files = [exe]
        print(f"  未指定文件，核验当前程序自身")

    bad = 0
    for p in files:
        if not p.is_file():
            print(f"\n  [跳过] {p}  不是文件")
            bad += 1
            continue
        got, size, err = _sha256_file(p)
        if err:
            print(f"\n  [失败] {p}  读取失败：{err}")
            bad += 1
            continue
        same = hmac.compare_digest(got, want)
        size_ok = (exp_size == 0) or (size == exp_size)
        print(f"\n  {p}")
        print(f"    实际哈希  {got}")
        print(f"    实际大小  {size} B")
        if same and size_ok:
            print("    [ok] 与签名一致，这就是官方发布的安装包")
        elif same and not size_ok:
            # 哈希覆盖全文，正常不可能只差大小。真出现说明期望值自身不自洽。
            print(f"    [!!] 哈希一致但大小不符（{size} vs {exp_size}），重新下载")
            bad += 1
        else:
            print("    [!!] 不通过 —— 这不是签名里那一份，不要运行它")
            bad += 1

    print("")
    if bad:
        print(f"  {bad} 个文件未通过")
        return 1
    print(f"  {len(files)} 个文件全部通过")
    return 0


def require_license() -> bool:
    """命令行入口的闸门。1.3.0 起只剩「强制更新」这一条能拦人。

    函数名保留：apply / revert / watch / verify 四条 CLI 路径都在调它。
    真实语义现在是「版本够不够新」，授权那层已经拆了。

    注意：_UPD 只在 GUI 里由 boot / update_now 填。CLI 是新进程，进来时
    是空的 —— 那样 hard_block() 永远 False，闸门形同不存在。所以这里
    先主动查一次（4 秒超时，失败放行）。
    """
    if not _UPD:
        set_update_state(update_check(force=True))
    blk, why = hard_block()
    if not blk:
        return True
    print(f"必须先更新：{why}")
    with _UPD_LOCK:
        r = dict(_UPD)
    if r.get("url"):
        print(f"  下载地址  {r['url']}")
    if r.get("sha256"):
        print(f"  SHA-256   {r['sha256']}")
    print(f"  当前版本  {VERSION}")
    print(f"  交流群    {brand.LINE}")
    return False


def cmd_license(code: str) -> int:
    """查看机器码 / 授权状态。

    1.3.0 起不用授权码了，传了 code 也只会被告知「不需要」—— 但命令不能
    报错退出，老用户手上还有 `xm license XXXX` 的记忆。
    """
    st = license_state(force=True)
    print(f"{APP_NAME} · 状态")
    print(f"  版本      {VERSION}")
    print(f"  授权      {st.get('msg')}（1.3.0 起所有人直接可用）")
    print(f"  机器码    {st.get('machine') or lic.machine_code()}")
    print(f"  更新服务  {st.get('server') or licnet.base_url(DATA_DIR)}")
    if code:
        print(f"\n忽略参数 {code!r}：不再需要授权码，删掉它直接跑即可。")
    return 0


#: dir 子命令里客户端名的别名。用户不会记得内部叫 wb。
DIR_ALIAS = {"codex": "codex", "wb": "wb", "workbuddy": "wb",
             "zcode": "zcode", "z": "zcode",
             "dsh": "dsh", "deepseek": "dsh"}


def cmd_dir(names, rest, forget: bool = False) -> int:
    """手动目录的命令行版本。窗口打不开时（远程、精简系统）只能走这条。

        xm dir                        看当前记录（含失效的）
        xm dir codex                  看 codex 的记录
        xm dir codex D:\\path          指定
        xm dir codex --forget         清掉，回到自动探测

    "codex D:\\path" 这种写法里，第一个词是客户端名，不是路径的一部分 ——
    上一版把它拼进路径了，于是每个目标都报「不是文件夹 → codex D:/path」。
    """
    words = [w for w in rest if w]
    want: list[str] = list(names or [])
    if not want and words and words[0].lower() in DIR_ALIAS:
        want.append(DIR_ALIAS[words[0].lower()])
        words = words[1:]
    elif not want and words and not forget:
        # 第一个词既不是客户端名，也不像一个存在的位置 —— 那就是拼错了。
        # 不拦的话会被当成路径，四个目标一起报「不是文件夹」，用户看不出
        # 到底是命令写错了还是路径写错了。
        head = words[0]
        looks_pathy = ("\\" in head or "/" in head or
                       head.startswith(".") or Path(head).is_dir())
        if not looks_pathy:
            print(f"不认识的客户端：{head}")
            print("可用：" + " / ".join(ORDER))
            print("指定目录：xm dir <客户端> <目录>")
            return 2
        # 看着像路径但没说是哪个客户端：一个目录不可能同时是四个客户端的
        # home。上一版照单全收，把 .codex 同时记给了 WB / ZCode / DSH，
        # 之后那三个目标全都指向错误位置。
        print("要给哪个客户端指定目录？")
        print("  xm dir <客户端> <目录>")
        print("  客户端：" + " / ".join(ORDER))
        return 2
    # 剩下的才是路径。整段拼回去是因为 Windows 路径里常有空格，argparse
    # 在没加引号时会把它们切成好几段。
    path = " ".join(words).strip()

    targets = resolve(want) if want else all_targets()
    if not targets:
        print(f"不认识的客户端：{' '.join(want)}")
        print("可用：" + " / ".join(ORDER))
        return 2

    changed = 0
    for t in targets:
        label = LABELS.get(t.name, t.name)
        raw = core_base.raw_root(t.name)
        if path:
            p = Path(path)
            if not p.is_dir():
                print(f"{label}: 不是文件夹 → {path}")
                continue
            cls = REGISTRY.get(t.name)
            if cls is not None:
                try:
                    ok, why = cls.looks_like(p)
                except Exception:                              # noqa: BLE001
                    ok, why = True, ""
                if not ok:
                    print(f"{label}: 不收 → {why}")
                    continue
            if save_root(t.name, str(p)):
                print(f"{label}: 已记录 {p}")
                changed += 1
            else:
                print(f"{label}: 写入失败")
        elif forget:
            if not raw:
                print(f"{label}: 本来就没记录")
                continue
            try:
                d = json.loads(core_base.PATHS_FILE.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.pop(t.name, None)
                    core_base.atomic_write(
                        core_base.PATHS_FILE,
                        json.dumps(d, ensure_ascii=False, indent=2))
                    print(f"{label}: 已清除 {raw}")
                    changed += 1
            except (OSError, ValueError) as e:
                print(f"{label}: 清除失败 {e}")
        else:
            if raw:
                print(f"{label}: {raw}")
            else:
                print(f"{label}: 自动探测")
    if not (path or forget) and not any(
            core_base.raw_root(t.name) for t in targets):
        print("\n都靠自动探测。指定某个客户端：xm dir codex <目录>")
    return 0 if (not path or changed) else 1


def cmd_apply(names, persona_path, dry: bool) -> int:
    persona = load_persona(persona_path)
    targets = resolve(names)
    if not targets:
        print("没有匹配的目标")
        return 1
    ok = 0
    for t in targets:
        rep = t.inject(persona, dry=dry)
        print("  " + rep.line())
        for s in rep.skipped[:5]:
            print(f"      · {s}")
        # 「无需改动」也是成功：目标已经是注入后的状态
        if rep.ok:
            ok += 1
    print(f"\n{'预演' if dry else '完成'}：{ok}/{len(targets)} 个目标就绪")
    if not dry and ok:
        print("没生效就关掉客户端重开——它只在启动时读一次")
    return 0 if ok else 1


def cmd_verify(names) -> int:
    """体检：只看磁盘，不动文件。"""
    persona = ""
    try:
        persona = load_persona()
    except SystemExit:
        pass
    bad = 0
    absent = 0
    for t in resolve(names):
        print(f"\n{LABELS.get(t.name, t.name)}")
        try:
            items = t.verify(persona)
        except Exception as e:                                 # noqa: BLE001
            items = [{"ok": False, "text": f"检查出错：{e}", "hint": ""}]
        if len(items) == 1 and items[0]["text"] == "未安装":
            absent += 1
        for it in items:
            # 用 ASCII 记号：控制台代码页是 936 时 ✓ 会编码失败
            print(("  [ok] " if it["ok"] else "  [!!] ") + it["text"])
            if not it["ok"] and it["text"] != "未安装":
                bad += 1
            if not it["ok"] and it.get("hint"):
                print("        " + it["hint"])
    tail = "全部就绪" if not bad else f"{bad} 项有问题"
    if absent:
        tail += f"，{absent} 个客户端未安装"
    print(f"\n体检完成：{tail}")
    print("文件层面通过 ≠ 模型一定照做。去客户端发一句「小熊猫自检」，"
          "回「自检成功」就是真的成了。")
    return 0 if not bad else 1


def cmd_revert(names) -> int:
    targets = resolve(names)
    n = 0          # 真的改了文件的客户端数
    failed = 0
    absent = 0     # 没装 / 没靶点，无事可做
    for t in targets:
        rep = t.revert()
        print("  " + rep.line())
        # 「未安装」不是失败。原来只有 not rep.ok 一个判据，未装的客户端
        # （found=False，ok 默认 False）会被算进 failed，用户看到
        # 「2 个目标已还原，1 个失败」，去翻目录找那个没还原干净的文件，
        # 其实那台机器上压根没装 Codex。失败只算「装了我们却还原不动」。
        if not rep.found:
            absent += 1
        elif not rep.ok:
            failed += 1
        elif rep.changed:
            n += 1
    # 汇总按「真改了东西」算。之前累加 rep.ok，重复点还原会报「已还原 4 个目标」，
    # 实际一个文件都没动 —— 用户以为出问题了（或者以为工具在他没注意时改了文件）。
    if failed:
        print(f"\n{n} 个目标已还原，{failed} 个失败")
    elif n:
        print(f"\n已还原 {n} 个目标")
    else:
        print("\n没有需要还原的注入")
    if absent:
        print(f"{absent} 个客户端未安装，跳过")
    return 0


def _stdout_is_console() -> bool:
    """stdout 是直接连着控制台，还是被重定向进了管道 / 文件。

    这个区分决定用哪种编码，弄反了就是乱码：
      · 连着控制台 → 必须用控制台代码页（中文机器上是 cp936）
      · 被重定向     → 必须用 UTF-8，因为接它的是别的程序 / 编辑器
    """
    try:
        h = ctypes.windll.kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        if not h or h == ctypes.c_void_p(-1).value:
            return False
        # GetFileType: 2 = FILE_TYPE_CHAR（控制台），3 = FILE_TYPE_PIPE
        return int(ctypes.windll.kernel32.GetFileType(
            ctypes.c_void_p(h))) == 2
    except Exception:                                          # noqa: BLE001
        return False


def _safe_stdio() -> None:
    """把标准输出调成「写什么都对」的编码。

    两个坑，必须一起处理：

    1. 控制台代码页不是 UTF-8 时，print 一个编不了的字符会抛
       UnicodeEncodeError，整个命令挂掉 —— 所以 errors 改成 replace。

    2. 往管道里写的时候不能跟着控制台代码页走。Git Bash 的 `2>&1`、
       VS Code 的输出面板、`| findstr` 全是管道，它们按 UTF-8 解字节。
       如果这边按 cp936 写，中文就成了 `ÒÑ¼ÇÂ¼` 这种乱码 —— 这就是
       用户在终端里看到的那串东西。

    所以：连着控制台 → 用控制台代码页；被重定向 → 用 UTF-8。
    """
    enc = _console_encoding() if _stdout_is_console() else "utf-8"
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            s.reconfigure(encoding=enc, errors="replace")
        except Exception:                                  # noqa: BLE001
            try:
                s.reconfigure(errors="replace")     # 老 Python 不认 encoding
            except Exception:                              # noqa: BLE001
                pass


def _console_encoding() -> str:
    """当前控制台输出代码页对应的 Python 编码名。

    中文 Windows 的 cmd 默认是 936（GBK）。如果按 UTF-8 往 CONOUT$ 写，
    控制台会按 GBK 解，中文全变乱码 —— 用户看到的就是一排问号和方块。
    读 GetConsoleOutputCP 跟着它走，写什么都是对的。
    """
    try:
        cp = int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:                                          # noqa: BLE001
        return "utf-8"
    if cp == 65001:
        return "utf-8"
    if cp == 0:
        return "utf-8"
    # Python 认 cp936 / cp437 / cp1252 这类名字，跟 Windows 的代码页号对得上
    return f"cp{cp}"


def attach_console() -> bool:
    """windowed 版 exe 从 cmd/powershell 里跑时，把输出接回父控制台。

    双击启动没有父控制台，AttachConsole 直接失败 —— 不影响 GUI。
    """
    try:
        k = ctypes.windll.kernel32
        if not k.AttachConsole(-1):                            # ATTACH_PARENT_PROCESS
            return False
        # 被重定向时（`xm status > out.txt`、`| findstr`、Git Bash 的 `2>&1`）
        # 不要走 CONOUT$。父进程已经把 stdout 换成管道/文件了，我们再往
        # CONOUT$ 写就是绕开重定向直接怼屏幕：重定向出来的内容是空的，
        # 屏幕上倒多出一份。编码也交给 _safe_stdio（重定向时用 UTF-8）。
        if not _stdout_is_console():
            return True
        # 真接在控制台上：按控制台代码页开流，错误降级成 ? 而不是抛异常。
        sys.stdout = open("CONOUT$", "w", encoding=_console_encoding(),  # noqa: SIM115
                          errors="replace", buffering=1)
        sys.stderr = sys.stdout
        return True
    except Exception:                                          # noqa: BLE001
        return False


def main(argv=None) -> int:
    _safe_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)

    # --forget 是 dir 子命令自己的开关。argparse 不认识它，会直接报
    # "unrecognized arguments" 退出 —— 所以在 parse_args 之前先摘出来，
    # 用一个普通标志带下去。摘完的 raw 才是真正要解析的那份参数。
    forget = "--forget" in raw
    if forget:
        raw = [x for x in raw if x != "--forget"]

    # 版本查询在 argparse 之前短路。
    #
    # 为什么不用 ap.add_argument("--version", action="version")：
    #   * 那样 --version 只在没有子命令时生效，`xm update --version` 会被
    #     位置参数吃掉，行为不一致；
    #   * windowed 版 exe 的 action="version" 走 argparse 自己的
    #     parser.exit() + stdout 写入，输出可能落在 attach 之前的悬空句柄上。
    # 直接在这里打一行再返回，不走任何分支 —— 版本查询必须在「条款未同意」
    # 「配置文件坏了」「没有靶点」这些状态下也能出结果，否则用户在报障时
    # 连自己装的是哪一版都说不出来。
    #
    # attach_console 必须在这之前：windowed exe 从 cmd 启动时进程本身没有
    # 控制台，先 print 等于往悬空句柄写，用户看到的是「什么都没输出、退出码 0」，
    # 比报错还难查。
    for flag in ("--version", "-v"):
        if flag in raw:
            if getattr(sys, "frozen", False):
                attach_console()
            print(VERSION)
            return 0

    # argparse 的小标题和内置错误文案是英文的（usage / positional arguments /
    # options / show this help message and exit / error: ...）。这个工具从界面
    # 到文档全是中文，帮助信息突然冒出几句英文很割裂，所以换成中文。
    # 必须在 ArgumentParser 构造之前调用：add_help 在构造时就把
    # "show this help message and exit" 的译文固化到 -h 选项上了。
    apply_zh()
    ap = argparse.ArgumentParser(prog="xm", description=APP_NAME)
    ap.add_argument("cmd", nargs="?", default="gui",
                    choices=["gui", "status", "apply", "revert", "verify",
                             "watch", "license", "dir", "update", "group",
                             "verify-download"])
    ap.add_argument("-t", "--target", action="append",
                    help="codex / wb / zcode / dsh，可重复")
    ap.add_argument("-p", "--persona", help="自定义人格文件")
    ap.add_argument("--dry", action="store_true", help="只算不写")
    ap.add_argument("-w", "--watch", action="store_true", help="常驻守护（无窗口）")
    ap.add_argument("--interval", type=int, default=60, help="守护检查间隔，秒")
    ap.add_argument("--accept", action="store_true",
                    help="免交互接受使用条款（脚本用）")
    # license 后面跟授权码；授权码里有 `.` 和中文前缀，不能当独立位置参数，
    # 所以用剩余参数收集再拼起来。dir 的目录路径也走这里（可能带空格）。
    ap.add_argument("rest", nargs="*", help="license 的授权码 / dir 的目录")
    a = ap.parse_args(raw)
    code = " ".join(a.rest).strip()

    # 控制台要在任何输出之前接上。
    #
    # windowed 版 exe 从 cmd / PowerShell 里启动时，进程本身不带控制台：
    # sys.stdout / sys.stdin 是 None 或已失效的句柄。以前只在 license 分支
    # 里 attach，其它命令（status / apply / verify ...）的前几行输出 ——
    # 包括同意条款那一屏 —— 全落在悬空句柄上：要么什么都没打出来，要么
    # input() 抛 RuntimeError 直接崩。把 attach 提到这里，后面所有分支
    # 都不用再操心这件事（GUI 分支里 attach 失败也无所谓，双击本来就没
    # 父控制台）。
    if getattr(sys, "frozen", False) and a.cmd != "gui":
        attach_console()

    # GUI 自己弹面板，走到那儿再校验；命令行模式（含守护）先过闸门。
    # license 例外：它在同意之前就得能看机器码 —— 用户拿不到机器码就没法
    # 去群里换授权码，卡在这儿等于把入口封死了。它只读硬件信息，不写任何
    # 客户端文件，风险为零。
    if a.cmd == "license":
        return cmd_license(code)

    # group / update 是只读命令：看群号、查版本。不需要同意条款也不改
    # 任何客户端文件 —— 用户拿不到群号就没法找人问，卡在这儿纯属添堵。
    if a.cmd == "group":
        # 命令行是用户主动来查，值得等一次网络（4 秒超时）。
        # 不拉的话，后台改了群号，GUI 能看到，命令行还是老的 —— 同一条信息
        # 两个来源，迟早有人按命令行抄了个旧群号。
        try:
            brand.refresh(DATA_DIR, force=True)
        except Exception:                                      # noqa: BLE001
            pass                     # 拉不到就用当前值，不能让查群号变成报错
        print(f"{brand.GROUP_NAME} {brand.GROUP_NO}")
        print(f"  加群  {brand.JOIN_URL}")
        if brand.TIP:
            print(f"  {brand.TIP}")
        if brand.status() == "remote":
            print("  来源  服务端下发（已验签）")
        elif brand.status() == "cached":
            print("  来源  本地缓存（已验签，服务端暂时连不上）")
        else:
            print("  来源  内置默认值")
        tp = brand.tamper()
        if tp:
            # 有 tamper 说明「服务端给了东西但验不过」。这时上面打的是
            # 回落后的值，必须让人知道，否则会以为后台改的没生效。
            print(f"  [注意] 远端内容校验失败：{tp}（上面显示的是回落值）")
        bad = brand.verify()
        if bad:
            for b in bad:
                print(f"  [异常] {b}")
            return 1
        return 0

    if a.cmd == "update":
        return cmd_update()

    # 安装包核验是只读命令：算哈希、比哈希，不写任何客户端文件。
    # 跟 group / update 同一类，不该要求先同意条款 —— 用户正是「还没敢跑
    # 这个 exe，想先确认它是不是官方的」才会用它。卡在条款前面，等于让
    # 人先运行了可疑文件才能验可疑文件。
    if a.cmd == "verify-download":
        return cmd_verify_download(a.rest)

    if a.cmd != "gui" or a.watch:
        if not require_consent(raw):
            return 2

    if a.watch or a.cmd == "watch":
        # 守护进程在启动时**不**同步查版本 —— 它是被计划任务/注册表在开机
        # 时拉起来的，那儿多等 4 秒会拖慢登录。强制更新由 watch_loop 内部
        # 每 30 分钟查一轮，命中就自己停手。这里只过条款（require_consent
        # + require_license 现在恒 True，留着是为了保持调用链一致）。
        return watch_loop(a.interval)

    if a.cmd == "gui":
        return gui()
    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "dir":
        return cmd_dir(a.target, a.rest, forget=forget)

    # 下面三条都会改磁盘或读客户端目录，必须已授权。
    # 例外：apply --dry 一个字节都不写，只是把「会改哪些靶点」算出来给用户看。
    # 预演是决策依据，不是收益 —— 要求先有授权码才能看预演，等于让用户在
    # 看不见结果的情况下先去群里要码，纯属添堵。所以 --dry 只过条款闸门
    # （上面已经过了），不查授权。
    if a.cmd in ("apply", "verify", "revert"):
        if not (a.cmd == "apply" and a.dry) and not require_license():
            return 3
    if a.cmd == "apply":
        return cmd_apply(a.target, a.persona, a.dry)
    if a.cmd == "verify":
        return cmd_verify(a.target)
    if a.cmd == "revert":
        return cmd_revert(a.target)
    # 走到这儿说明命令拼错了。以前这里直接 return cmd_revert(...) ——
    # 敲错一个字母就把客户端还原了，用户还以为是别的操作。
    print(f"不认识的命令：{a.cmd}")
    print("可用：gui / status / apply / verify / revert / license / watch / "
          "update / group / dir / verify-download")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                                          # noqa: BLE001
        crash_log(traceback.format_exc())
        raise
