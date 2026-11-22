"""小熊猫破甲 · 注入引擎内核。

一个客户端一个适配器。这层只提供四件事：
    · 标记段     —— 我们写的块用固定头尾包起来，还原时按标记切除
    · 原子写     —— 先写临时文件再 os.replace，中途断电不会留半份
    · 备份/还原  —— 第一次改之前留原始副本，永不覆盖
    · 靶点探测   —— 装在哪、支持到哪一层，探测结果如实报告

设计原则：**探测不到就不动手**。宁可报「没找到靶点」，也不写一个自己都不确定
位置对不对的补丁 —— 客户端是几百 MB 的 Electron 包，改坏了直接起不来。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

def _home() -> Path:
    """用户目录。`Path.home()` 在某些环境下会抛 RuntimeError。

    Windows 上它读 `USERPROFILE`，读不到再退 `HOMEDRIVE` + `HOMEPATH`，
    两个都没有就抛 —— 精简系统、被组策略锁过的机器、某些远程会话里真的
    会这样。那一抛发生在 import 期，等于整个程序起不来，用户看到的是
    双击没反应。这里逐级兜底，最差退到当前目录（后面 DATA_DIR 还会再兜）。
    """
    try:
        p = Path.home()
        if str(p):
            return p
    except (RuntimeError, OSError):
        pass
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            try:
                p = Path(v)
                if p.is_dir():
                    return p
            except (OSError, ValueError):
                continue
    drive, tail = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if drive and tail:
        try:
            return Path(drive + tail)
        except (OSError, ValueError):
            pass
    return Path(os.getcwd())


HOME = _home()


def _pick_data_dir() -> Path:
    """挑一个能建出来的目录存备份和状态。

    `~/.xiaoxiongmao` 是首选，**只要它建得出来就一定是它**。这一点比
    「能不能写」更重要：这个目录决定备份、state.json、paths.json 住在
    哪儿，一旦两次启动挑到不同目录，用户会看到「已破甲」状态凭空消失、
    备份列表空掉 —— 比写不进去更难查。

    只有连目录都建不出来时才换地方：用户目录被设成只读、配置文件被重定向
    到网络盘、杀软按名字拦目录。换的顺序是 LOCALAPPDATA → APPDATA →
    TEMP → 当前目录，最后一个基本必然可用。
    """
    env = os.environ.get("XM_DATA")
    if env:
        d = Path(env)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError):
            pass                     # 测试用的隔离目录，建不出来也照用
        return d

    primary = HOME / ".xiaoxiongmao"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except (OSError, ValueError):
        pass

    for k in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP"):
        v = os.environ.get(k)
        if not v:
            continue
        d = Path(v) / "XiaoXiongMao"
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except (OSError, ValueError):
            continue
    try:
        d = Path(os.getcwd()) / ".xiaoxiongmao"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except (OSError, ValueError):
        return primary


DATA_DIR = _pick_data_dir()
BACKUP_ROOT = DATA_DIR / "backups"
STATE_FILE = DATA_DIR / "state.json"

# 标记段用纯文本分隔线：.tpl / .md / .txt / json 里都合法，肉眼可见也好删。
# 不用 HTML 注释 —— 提示词模板最终是给模型读的纯文本，注释标记在某些渲染
# 环节会被吃掉，切除时反而找不到尾。
BEGIN = "========== xm-persona:begin =========="
END = "========== xm-persona:end =========="

#: 早期版本用 HTML 注释，读入时一并清掉，避免旧块叠在新块上面
LEGACY = [
    ("<!-- xm:begin v1 -->", "<!-- xm:end -->"),
    ("<!-- xm:begin -->", "<!-- xm:end -->"),
]

NL = "\n"


# ---------------------------------------------------------------- 文件读写

def read_text(p: Path) -> str:
    """读文本，绕过编码坑。BOM 先剥掉 —— TOML/JSON 都不允许 BOM。"""
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    return decode_bytes(raw)


def _looks_utf16(raw: bytes) -> bool:
    """判断是不是**无 BOM** 的 UTF-16 —— 只看字节形态，不解码。

    为什么需要它：`raw.decode("utf-16")` 在没有 BOM 时按本机字节序
    （Windows 上是小端）解，任何偶数长度字节都「解得成功」—— 包括纯 ASCII
    文本，解出来是一串汉字乱码，而且不报错。所以不能靠「试解码成功」判断。

    真 UTF-16 的判据是**零字节的分布**：ASCII 范围内每个字符占两个字节，
    高字节是 `\\x00`。中文（U+4E00-U+9FFF）两个字节都非零，但它们之间
    总夹着 ASCII 的换行、空格、标点。

    判法：取前 4 KB，若长度是偶数、并且零字节**几乎都落在奇数位**
    （小端）或**几乎都落在偶数位**（大端），就认为是 UTF-16。
    纯 UTF-8 / GBK 文本里几乎没有零字节，会直接被排除。

    实测踩过的坑：无 BOM 的 UTF-16LE `# 我的人格\\n` 是
    `23 00 20 00 11 62 ...`，旧 `decode_bytes()` 走 `utf-8`（成功，
    因为是合法单字节序列）→ 得到 `#\\x00 \\x00\\x11b` 这种带 NUL 的垃圾，
    然后连同人格块一起写回，**用户的原文就彻底毁了**。
    """
    head = raw[:4096]
    if len(head) < 4 or len(head) % 2:
        return False
    zeros = [i for i, b in enumerate(head) if b == 0]
    n = len(zeros)
    if n < 2:
        return False
    # 零字节占比太低（< 8%）不像 UTF-16 文本；太高（> 50%）多半是二进制
    ratio = n / len(head)
    if ratio < 0.08 or ratio > 0.5:
        return False
    odd = sum(1 for i in zeros if i % 2)
    even = n - odd
    # 小端：ASCII 的高字节在奇数位；大端：在偶数位。允许 15% 噪声
    return odd >= n * 0.85 or even >= n * 0.85


def decode_bytes(raw: bytes) -> str:
    """字节 → 文本。按「最可能是哪种编码」的顺序试。

    utf-16 要在 utf-8 之前判：UTF-16LE 的 ASCII 文本是 `#\\x00 \\x00` 这种
    形态，用 utf-8 解不会报错（全是合法单字节），解出来是 `#\\x00 \\x00`
    —— 一串带 NUL 的垃圾。所以带 BOM 的必须先认。

    无 BOM 的也要先认（`_looks_utf16`），否则同样的垃圾会被当成合法
    UTF-8 收下，注入时再原样写回 —— 那是把用户的文件写坏，不是「兼容」。
    `latin1` 放到最后：它对任何字节都「成功」，兜底可以，提前试等于放弃判断。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if _looks_utf16(raw):
        # 无 BOM：按零字节分布定字节序。小端是 Windows 上的常态。
        enc = "utf-16-le"
        zeros = [i for i, b in enumerate(raw[:4096]) if b == 0]
        if zeros and sum(1 for i in zeros if i % 2 == 0) > len(zeros) * 0.85:
            enc = "utf-16-be"
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return raw.decode("utf-16")
    except UnicodeDecodeError:
        pass
    return raw.decode("latin1")


def detect_encoding(p: Path) -> str:
    """猜这个文件当前是什么编码。猜不出来返回 utf-8。

    只用来「按原样写回」，不追求精确 —— 判错了最差是文件编码变一次，
    而写不进去是功能直接失效。
    """
    try:
        raw = p.read_bytes()
    except OSError:
        return "utf-8"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if b"\x00" in raw[:4096]:
        # 没有 BOM 却有 NUL：多半是无 BOM 的 UTF-16LE
        return "utf-16"
    for enc in ("utf-8", "gbk"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _clear_readonly(p: Path) -> None:
    """去掉只读属性。

    Windows 上 `os.replace` 覆盖只读文件会直接 PermissionError。用户从
    别的机器拷来的 config.toml、从压缩包解出来的 AGENTS.md 经常带只读位，
    这不是「权限不足」，是文件属性 —— 一个 chmod 就能改，不该报成失败。

    **不能用 `os.access(p, os.W_OK)` 判断**：Windows 上它只看 ACL，不看
    FILE_ATTRIBUTE_READONLY 位 —— 文件明明带只读属性，`os.access` 照样
    返回 True，于是这里跳过 chmod，随后 `os.replace` 抛 PermissionError。
    正确做法是无条件清一次属性位（对非只读文件是无副作用的幂等操作）。
    """
    if not p.exists():
        return
    try:
        st = p.stat()
        # S_IWRITE 在 Windows 上映射到 FILE_ATTRIBUTE_READONLY 的反面。
        # 保留原有权限位，只补写权限 —— 不要用 0o666，那会把执行位抹掉。
        os.chmod(p, st.st_mode | 0o200)
    except (OSError, ValueError):
        pass


def atomic_write(p: Path, text: str, *, newline: str = NL,
                 encoding: str = "utf-8") -> None:
    """写临时文件 → fsync → os.replace。

    临时名带 pid + 线程 id + 随机后缀：同一进程里两个线程写同一个文件
    （比如并发点破甲）时，只用 pid 会撞名，后一个把前一个的临时文件覆盖掉，
    os.replace 就会搬走半份内容。随机后缀再兜一层，成本可忽略。

    只读属性和「文件被短暂占用」都在这里处理，因为它们是用户环境里最常见的
    两种写失败：杀软扫描窗口期持一下句柄、文件带只读位。两种都不该报成
    「破甲失败」—— 用户重试一次就能成，但我们得替他重试。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    _clear_readonly(p)
    tmp = p.with_name(
        f".{p.name}.xmtmp{os.getpid()}_{threading.get_ident()}_{secrets.token_hex(3)}")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # 占用类失败（杀软、索引器、正在保存同一文件的编辑器）重试几次。
        # 退避 0.08/0.16/0.32 s，总耗时不到 0.6 s —— 比让用户看到「写失败」
        # 再自己点一次快得多，也不会把界面卡住（一次注入几十毫秒都嫌多）。
        last: OSError | None = None
        for i, wait in enumerate((0, 0.08, 0.16, 0.32)):
            try:
                os.replace(tmp, p)
                return
            except PermissionError as e:
                last = e
                if i < 3:
                    time.sleep(wait)
        if last:
            raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_like(p: Path, text: str) -> None:
    """**注入**用：统一写 UTF-8。

    这里原本按目标文件的原有编码写回（GBK 文件还是 GBK），出发点是不改变
    用户的文件。但实测（1.3.7）证明这个出发点选错了：

        GBK 的 AGENTS.md 注入后，整份文件仍是 GBK。
        `raw.decode("utf-8")` → UnicodeDecodeError: 0xce
        `raw.decode("gbk")`   → 正常，人格确实在里面

    而 ZCode / DSH / Codex 都是 Node / Electron 客户端，读用户级指令文件
    一律 `fs.readFileSync(p, "utf8")` —— 遇到 GBK 字节会得到一串 `锟斤拷`，
    人格等于没注入，界面还报「已破甲」。**写得进去但客户端读不出来，是比
    写失败更糟的失败**：用户不会重试，只会觉得这工具是骗人的。

    所以注入路径统一 UTF-8（所有客户端都认），编码还原交给 revert ——
    备份里存的是原始字节，还原时按原始编码写回，用户的文件一个字节都不变。

    还原路径改用 `write_orig()`，不要用这个函数。
    """
    atomic_write(p, text, encoding="utf-8")


def write_orig(p: Path, raw: bytes) -> None:
    """**还原**用：把备份里的原始字节原样写回。

    备份存的是注入前的字节（`Backup.save()` 落的是 `_strip_for_backup()`
    的结果，已经剥掉标记段），所以这里不重新编码、不猜编码 —— 直接搬字节。
    用户的 GBK 文件还原后还是 GBK，UTF-16 还原后还是带 BOM 的 UTF-16。

    这是「注入改编码、还原还编码」的另一半。只做前一半会让用户的文件
    永久变成 UTF-8；只做后一半则人格读不出来。两半必须同时在。
    """
    _atomic_write_bytes(p, raw)


def _atomic_write_bytes(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    _clear_readonly(p)
    tmp = p.with_name(
        f".{p.name}.xmtmp{os.getpid()}_{threading.get_ident()}_{secrets.token_hex(3)}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        last: OSError | None = None
        for i, wait in enumerate((0, 0.08, 0.16, 0.32)):
            try:
                os.replace(tmp, p)
                return
            except PermissionError as e:
                last = e
                if i < 3:
                    time.sleep(wait)
        if last:
            raise last
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def digest(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def file_sha(p: Path) -> str:
    try:
        return digest(p.read_bytes())
    except OSError:
        return ""


def is_writable(p: Path) -> bool:
    """能不能写。文件不存在时看父目录。"""
    if not p.exists():
        d = p.parent
        while not d.exists() and d != d.parent:
            d = d.parent
        return os.access(d, os.W_OK)
    return os.access(p, os.W_OK)


# ---------------------------------------------------------------- 标记段

def _cut(text: str, begin: str, end: str) -> str:
    """切掉 begin..end 之间的内容（含两端）。

    只有头没有尾的情况要**切到末尾**，不能原地保留。原因：注入是「先算好
    整段文本，再一次性 os.replace」，理论上不会留半块；但用户手动编辑、
    老工具截断、编辑器保存时掉内容都会造出「有头无尾」。这种残块留着的话，
    下一次注入会在它后面再叠一块 —— 字面量 `BEGIN` 出现两次，而 cut 只认
    第一个 BEGIN 到第一个 END，第二次的 END 会被当成第一个的尾，切割范围
    错位，人格就叠成了两份。切到末尾等于「认这块是坏的，整段扔掉重写」。
    """
    out, buf = [], text
    while True:
        i = buf.find(begin)
        if i < 0:
            out.append(buf)
            break
        out.append(buf[:i])
        j = buf.find(end, i)
        if j < 0:
            break                      # 只有头没有尾：丢掉头之后的一切
        buf = buf[j + len(end):]
    return "".join(out)


def strip_block(text: str) -> str:
    """切掉所有标记段（含旧版 HTML 标记），顺带收拾连续空行。"""
    s = text
    for b, e in [(BEGIN, END)] + LEGACY:
        s = _cut(s, b, e)
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s.rstrip() + ("\n" if s.strip() else "")


def put_block(text: str, body: str, *, where: str = "end") -> str:
    """把 body 用标记段包好后放进 text。已有段先切掉。"""
    base = strip_block(text)
    block = f"{BEGIN}\n{body.rstrip()}\n{END}\n"
    if where == "top":
        return block + ("\n" + base if base.strip() else "")
    return (base.rstrip() + "\n\n" if base.strip() else "") + block


def has_block(text: str) -> bool:
    """这个文件里有没有我们的注入痕迹（含残块）。

    判据只管**开头标记**在不在，不要求尾也在。理由：尾丢了恰恰是最需要
    处理的情形 —— 有头无尾说明上次写到一半被打断（断电、任务管理器结束
    进程、编辑器异常保存）。这种文件必须被当成「已注入」：

      · `revert()` 看到 False 会直接 `continue` 跳过，残块永远清不掉；
      · `inject()` 的 `put_block` 虽然也会切，但界面上的「已破甲」状态
        和 `verify()` 的自检都会因为 False 而报告不一致。

    旧实现要求 `BEGIN in text and END in text`，于是残块既不算注入、也
    还原不掉，用户只能手动删文件。
    """
    if BEGIN in text:
        return True
    return any(b in text and e in text for b, e in LEGACY)


# ---------------------------------------------------------------- 策略段替换

#: WorkBuddy 主提示词里的安全策略段。这一段的长度和位置在多个版本里都稳定，
#: 是唯一能整段替换而不破坏 Jinja 结构的地方。
CP_OPEN = "<content_policy>"
CP_CLOSE = "</content_policy>"

#: 老注入器留下的头标记。它们没有闭合标记，整段人格直接糊在文件里，
#: 只能从头标记一路切到末尾 —— 那不是原文，是旧注入。
LEGACY_HEAD = re.compile(
    r"<!--\s*(unlock-v\d+|pojia[\w.-]*|pj[\w.-]*|xiaoxiongmao[\w.-]*)", re.I)


def scrub_legacy(text: str) -> str:
    """切掉老版本注入器写的无标记人格块。

    特征：`<!-- unlock-v6:hash -->` 这类注释头，后面跟几十 KB 人格正文，
    一直到文件末尾。原文在这个位置之前是完整的。
    """
    m = LEGACY_HEAD.search(text)
    if not m:
        return text
    return text[:m.start()].rstrip() + "\n"


def put_policy(text: str, body: str, base: str = "") -> str:
    """把 <content_policy> 段整体换成注入块。

    老版本的注入工具直接用 persona 覆盖这一段的正文，而且不留标记 —— 结果
    文件里同时躺着两份人格，群号互相打架，原文的 Jinja 片段也一起没了。

    base 是能找到的干净原文（老工具留的 `prompt.tpl.unlockbak` 之类）。有它
    就以它为底 —— 既清掉旧块，也把被旧工具吃掉的那两万多字原文找回来。
    """
    if base.strip() and CP_OPEN in base and not residue(base):
        text = base
    else:
        text = scrub_legacy(text)

    i = text.find(CP_OPEN)
    if i < 0:
        return put_block(text, body)
    head = text[:i]
    after = text[i + len(CP_OPEN):]
    j = after.find(CP_CLOSE)
    tail = after[j + len(CP_CLOSE):] if j >= 0 else ""
    block = f"\n{BEGIN}\n{body.rstrip()}\n{END}\n"
    return f"{head}{CP_OPEN}{block}{CP_CLOSE}{tail}"


def residue(text: str) -> list[str]:
    """注入后自检：老工具的特征还在不在。"""
    bad = []
    for k in ("legacy-tool-a", "你已被圈", "100000001", "xmtoken", "破甲自检"):
        if k in text:
            bad.append(k)
    return bad


def fingerprint(persona: str) -> str:
    """从人格正文里取一句最有辨识度的，用来确认「写进去的确实是这份人格」。

    跳过标题行和短行 —— 那些在别的人格里也可能出现。
    """
    for line in persona.splitlines():
        s = line.strip().lstrip("#").strip()
        if len(s) >= 14 and not s.startswith("```"):
            return s[:32]
    return persona.strip()[:32]


def _short(p: Path) -> str:
    """给人看的短路径。版本号目录用上一层名字代替。"""
    parent = p.parent.name
    if re.match(r"^\d+\.\d", parent) and p.parent.parent.name:
        return f"{p.parent.parent.name}/{p.name}"
    return p.name


# ---------------------------------------------------------------- 备份

def _strip_for_backup(raw: bytes) -> bytes:
    """备份落盘前先剥掉注入痕迹。

    只处理纯文本；解不出来（二进制、异常编码）就原样返回 —— 备份层的职责是
    「存个能拿回去的副本」，不是做编码清洗，剥不动就宁可保留全部字节。
    """
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if not has_block(text):
            return raw                 # 没有标记段，原字节直接用，省一次编码往返
        try:
            # 先按标记段切，再清掉旧代注入器留下的无尾块
            clean = scrub_legacy(strip_block(text))
            out = clean.encode(enc)
        except (UnicodeEncodeError, ValueError):
            return raw
        # 剥完是空的说明原文本来就没有内容（zcode/dsh 的 AGENTS.md 常见），
        # 记成空文件比留一份注入态更接近真相
        return out
    return raw


@dataclass
class Backup:
    """一次注入 = 一个备份目录。里面有 manifest.json 和原文件副本。"""
    root: Path
    entries: dict[str, dict] = field(default_factory=dict)
    #: 备份层自己出的问题（没写成、磁盘满）。上层读它决定要不要提示用户，
    #: 不参与「注入成功与否」的判断。
    note: str = ""

    def save(self, target: str, path: Path) -> None:
        """把 path 的当前内容存进备份。

        两个坑都在这儿堵：

        1. 同一文件只存第一次 —— 一次注入里多个靶点可能指向同一个文件。
        2. **存之前先把我们自己的标记段剥掉**。`new_backup()` 每次注入都新建
           目录，Backup 实例的去重只在单次注入内有效；第二次注入时目标文件里
           已经是注入态，直接存就会把「注入后」当成「原始」存进去，之后
           revert 拿它写回等于原地覆盖，人格还在，界面却报「已还原」。
           剥一次的成本是一次正则扫描，换来 revert 真的能还原。

        备份失败不抛。磁盘满、目录被占、杀软锁文件都会让 write_bytes 抛
        OSError —— 如果让它冒出去，这一次注入直接算失败，可配置文件本身
        明明还能写。备份的定位是「额外一层保险」，不是前置条件；失败时
        把原因挂在 `self.note` 上，让上层能提示用户「这次没有备份」。
        """
        key = str(path)
        if key in self.entries:
            return
        try:
            raw = path.read_bytes()
        except OSError:
            # 原文件不存在：记下来，还原时把它删掉。
            # target 必须留 —— find_target_backup 靠它认领，少了就永远还原不了。
            self.entries[key] = {"exists": False, "target": target}
            return

        data = _strip_for_backup(raw)
        dst = self.root / "files" / f"{len(self.entries):02d}_{path.name}"
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        except OSError as e:
            # 记成 exists=True 但没备份文件，还原时会走「按标记段剥离」那条
            # 主路径（不依赖备份），所以功能仍然成立。note 留给界面。
            self.note = f"备份没写成（{getattr(e, 'strerror', None) or e}）"
            self.entries[key] = {"exists": True, "target": target,
                                 "backup": "", "sha": "", "size": 0}
            return
        self.entries[key] = {
            "exists": True, "backup": str(dst.relative_to(self.root)),
            "sha": digest(data), "size": len(data), "target": target,
        }

    def flush(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write(self.root / "manifest.json", json.dumps(
                self.entries, ensure_ascii=False, indent=2, sort_keys=True))
        except OSError as e:
            # manifest 写不出来 = 这份备份事后翻不了账，但文件副本已经落盘了。
            # 不让它把整次注入拽挂：还原主路径不读 manifest（按标记段剥离）。
            self.note = self.note or f"备份清单没写成（{getattr(e, 'strerror', None) or e}）"

    def original_of(self, path: Path) -> Optional[bytes]:
        """取这个文件在注入前的原始字节。没有记录、记录坏了、文件丢了都返回 None。

        用途是「注入覆盖了用户原有文件」这种情形 —— 典型是 Codex 的
        `xm-persona.md`：用户可能先手写一份放那儿，我们注入时把它覆盖掉。
        还原就必须把这份还回去，而不是删掉。

        为什么不能只靠「内容像不像我们的」判断：判成「不是我们的」时，
        文件里装的是我们注入进去的人格（因为被我们覆盖了），用户的原文
        已经不在磁盘上了 —— 只有备份里有。所以先查备份，查不到再退回
        删除逻辑。
        """
        meta = self.entries.get(str(path))
        if not isinstance(meta, dict):
            return None
        if not meta.get("exists"):
            return None        # 注入前不存在 → 还原时删掉，不该写回内容
        rel = meta.get("backup") or ""
        if not rel:
            return None        # 备份没写成，只剩 exists=True 的占位
        try:
            p = self.root / rel
            if p.is_file():
                return p.read_bytes()
        except (OSError, ValueError):
            pass
        return None


def new_backup() -> Backup:
    """开一份新备份。目录建不出来时退到一个临时目录，绝不抛。

    `BACKUP_ROOT.mkdir()` 会失败的情形不少：磁盘满、目录被别的进程占着、
    杀软把 `~/.xiaoxiongmao/backups` 拦下来、用户在「备份」列表里点清理的
    那一刻另一个线程正在重建目录。这些都不该让注入失败 —— 人格写得进
    config.toml，凭什么因为备份建不出来就不干。

    退到系统临时目录：那儿的生命周期不保证（重启可能被清），所以
    `Backup.note` 会带一句，界面看到就知道「这次备份不牢靠」。
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    try:
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        root = BACKUP_ROOT / ts
        n = 1
        while root.exists():
            root = BACKUP_ROOT / f"{ts}-{n}"
            n += 1
        root.mkdir(parents=True, exist_ok=True)
        return Backup(root)
    except OSError:
        pass

    import tempfile
    try:
        d = Path(tempfile.mkdtemp(prefix="xm-bk-"))
        return Backup(d, note="正式备份目录不可写，备份暂存在临时目录")
    except OSError:
        # 连临时目录都开不出来：给一个不落盘的 Backup。注入照常进行，
        # 只是这次没有备份 —— 还原靠标记段剥离，仍然成立。
        return Backup(BACKUP_ROOT / ts,
                      note="没有可用目录存放备份，本次未生成备份")


def latest_backup() -> Optional[Backup]:
    if not BACKUP_ROOT.exists():
        return None
    dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()])
    if not dirs:
        return None
    root = dirs[-1]
    mf = root / "manifest.json"
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Backup(root, data)


def find_target_backup(name: str) -> Optional[Backup]:
    """找这个目标最近一次被备份的记录。

    一次「一键破甲」会给每个目标各建一个备份目录，`latest_backup()` 只看最新
    的那个，拿它还原多目标里的第一个必然找不到记录。按 target 从新到旧扫。
    """
    if not BACKUP_ROOT.exists():
        return None
    try:
        dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()], reverse=True)
    except OSError:
        return None
    for d in dirs:
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(v.get("target") == name for v in data.values()):
            return Backup(d, data)
    return None


# ---------------------------------------------------------------- 备份列表 / 回滚

def _pretty_time(name: str) -> str:
    """20261121-165926 → 2026-11-21 16:59:26"""
    try:
        d, t = name.split("-")[:2]
        t = t.split("-")[0]
        return (f"{d[0:4]}-{d[4:6]}-{d[6:8]} "
                f"{t[0:2]}:{t[2:4]}:{t[4:6]}")
    except (ValueError, IndexError):
        return name


def list_backups() -> list[dict]:
    """所有备份，新的在前。读不出 manifest 的跳过。"""
    out: list[dict] = []
    if not BACKUP_ROOT.exists():
        return out
    try:
        dirs = [d for d in BACKUP_ROOT.iterdir() if d.is_dir()]
    except OSError:
        return out
    for d in sorted(dirs, key=lambda x: x.name, reverse=True):
        try:
            data = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data:
            continue                       # 老版本留下的空备份，不显示
        targets = sorted({v.get("target") for v in data.values() if v.get("target")})
        size = 0
        for v in data.values():
            try:
                size += int(v.get("size") or 0)
            except (TypeError, ValueError):
                pass
        out.append({
            "id": d.name,
            "time": _pretty_time(d.name),
            "targets": targets,
            "files": len(data),
            "size": size,
        })
    return out


def prune_backups(keep: int = 10) -> tuple[int, int]:
    """只留最近 keep 份备份，返回 (删了几个, 释放字节)。

    每份备份就是几个几十 KB 的文本，攒多了列表会很长，但删错了没法回退 ——
    所以只在用户明确点「清理」时调，不自动跑。
    """
    if not BACKUP_ROOT.exists():
        return 0, 0
    try:
        dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()],
                      key=lambda x: x.name, reverse=True)
    except OSError:
        return 0, 0
    removed = freed = 0
    for d in dirs[max(0, keep):]:
        try:
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                removed += 1
                freed += size
        except OSError:
            continue
    return removed, freed


def restore_backup(bid: str) -> tuple[int, list[str]]:
    """把某个备份里的文件逐字节写回去。返回 (还原数, 失败说明)。

    bid 只接受已知目录名，避免用 ../ 之类的东西把文件写到别处。
    """
    if not bid or "/" in bid or "\\" in bid or ".." in bid:
        return 0, ["备份名不合法"]
    root = BACKUP_ROOT / bid
    if not root.is_dir():
        return 0, ["找不到这个备份"]
    try:
        data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, ["备份清单损坏"]

    n, bad = 0, []
    for path_str, meta in data.items():
        p = Path(path_str)
        try:
            if meta.get("exists"):
                # manifest 里的 backup 是相对文件名。拼完再核一次它没跑出
                # 备份目录 —— manifest 是我们自己写的，但它躺在用户能改的
                # 目录里，一个 "../../" 就能让回滚去读别的文件。
                src = (root / str(meta["backup"])).resolve()
                try:
                    src.relative_to(root.resolve())
                except ValueError:
                    bad.append(f"{p.name}: 备份路径越界")
                    continue
                blob = src.read_bytes()
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(f".{p.name}.xmrestore")
                tmp.write_bytes(blob)
                os.replace(tmp, p)
            elif p.exists():
                p.unlink()
            n += 1
        except (OSError, KeyError) as e:
            bad.append(f"{p.name}: {getattr(e, 'strerror', None) or e}")
    return n, bad


# ---------------------------------------------------------------- 状态

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    """原子写。state.json 是每次注入都要改的热文件，写到一半断电就成了
    坏 JSON —— 而 persona_info() 和界面都读它，坏了整张人格卡没数据。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(STATE_FILE, json.dumps(st, ensure_ascii=False, indent=2))
    except OSError:
        # 状态写不进去不该让整次注入失败 —— 注入本身已经成功了
        pass


# ---------------------------------------------------------------- 手动指定目录

PATHS_FILE = DATA_DIR / "paths.json"


def has_any(p: Path, names: "tuple[str, ...] | list[str]") -> bool:
    """p 下有没有这些名字的条目（文件或目录都算）。只查一层，不递归。

    每个适配器拿它拼自己的「像不像」判断 —— 用户手动指定目录时，
    选错文件夹的概率比选对的概率高得多（弹出来默认停在「文档」）。
    """
    for n in names:
        try:
            if (p / n).exists():
                return True
        except OSError:
            continue
    return False


def raw_root(name: str) -> str:
    """paths.json 里记的原样字符串，不做任何校验。

    界面要用它判断「这个目标有没有手动目录」—— 包括记下来但已经失效的
    那种。saved_root() 会把失效的当成 None，界面就分不清「没选过」和
    「选过但选错了」，也就没法给出「换目录 / 忘记」这两个动作。
    """
    try:
        d = json.loads(PATHS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""
    return str(d.get(name) or "")


def manual_root(name: str, check=None) -> tuple[Optional[Path], str]:
    """取用户手动指定的目录，顺手校验它像不像。

    返回 (目录, 不像时的原因)。目录不存在也算「不像」—— 客户端卸载后
    路径还留在配置里，这时候如果当成「已安装」，界面会显示一个看着正常
    的状态，用户点破甲却什么也写不进去。
    """
    raw = raw_root(name)
    if not raw:
        return None, ""
    try:
        p = Path(raw)
        is_dir = p.is_dir()
    except (OSError, ValueError):
        return None, "记住的目录路径不合法，重新选一次"
    if not is_dir:
        return None, f"记住的目录已经不存在：{raw}"
    if check is not None:
        try:
            ok, why = check(p)
        except Exception:                                      # noqa: BLE001
            return p, ""                # 校验自己出错，不拦用户
        if not ok:
            return None, why or "这个目录不像对应的客户端目录"
    return p, ""


def saved_root(name: str) -> Optional[Path]:
    """用户手工指定的安装目录。自动探测扫不到时的最后一条路。"""
    raw = raw_root(name)
    if not raw:
        return None
    try:
        p = Path(raw)
        return p if p.is_dir() else None
    except (OSError, ValueError):
        return None


def save_root(name: str, path: str) -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(PATHS_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d[name] = path
    try:
        atomic_write(PATHS_FILE, json.dumps(d, ensure_ascii=False, indent=2))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 结果

@dataclass
class Hit:
    """一个靶点。path 是文件，how 说明怎么改，note 给人看。"""
    path: Path
    how: str                     # block / text / key
    note: str = ""


@dataclass
class Report:
    target: str
    ok: bool
    found: bool
    hits: list[Hit] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    note: str = ""

    def line(self) -> str:
        if not self.found:
            return f"{self.target}: 未安装"
        if self.note:
            return f"{self.target}: {self.note}"
        if not self.ok:
            return f"{self.target}: 失败"
        if self.changed:
            return (f"{self.target}: {len(self.changed)} 处已改"
                    + (f"，{len(self.skipped)} 处跳过" if self.skipped else ""))
        if not self.hits:
            return f"{self.target}: 没有可写的靶点"
        return f"{self.target}: 无需改动"


class Target:
    """适配器基类。子类只需实现 detect / collect / body。"""

    name = ""
    label = ""
    #: 进程名（小写）。宿主开着的时候改了文件也不会立刻生效，界面要提示重启。
    procs: tuple[str, ...] = ()
    #: 探测到的安装根目录
    root: Optional[Path] = None
    #: 手动目录没通过校验时，这里留一句给人看的话。probe() 在「没装」时
    #: 拿它当 detail —— 不然用户选错了目录，界面只显示「未安装」，
    #: 看不出是自己选错还是真没装。
    manual_note = ""
    #: 这次探测**真的用了**手动目录吗。为 False 而 manual_note 非空，
    #: 说明用户的指定被拒了、界面显示的是自动探测结果 —— 这件事必须让
    #: 用户知道，否则他以为自己选的目录生效了。
    manual_used = False

    @classmethod
    def looks_like(cls, p: Path) -> tuple[bool, str]:
        """这个目录像不像本客户端的目录。手动指定时用来防选错。

        返回 (像不像, 不像时的一句话原因)。基类不校验 —— 没有特征的
        适配器宁可放过，也不要因为一条规则把正常用法卡死。
        """
        return True, ""

    def detect(self) -> bool:
        raise NotImplementedError

    def collect(self) -> list[Hit]:
        """列出所有要改的文件。只读，不动磁盘。"""
        raise NotImplementedError

    def key(self) -> str:
        """写进 config 用的键名（可选）。"""
        return ""

    def body(self, persona: str) -> str:
        """块内容。默认就是人格正文。"""
        return persona

    def fingerprint_of(self, persona: str) -> str:
        """「写进文件的那份正文」里的指纹，用来确认落盘的确实是这份人格。

        默认跟原文一致。改了正文的适配器（wb 要打散 Jinja 定界符）必须覆盖：
        指纹在原文里取、却拿去打散后的文本里找，永远找不到 —— 体检会报
        「注入的不是这份人格」，用户重跑多少次都是同一句。
        """
        return fingerprint(persona)

    def clean_base(self, h: Hit) -> str:
        """干净原文。子类可以覆盖：同一目录下老工具留下的 .unlockbak / .bak。"""
        return ""

    def is_applied(self, h: Hit) -> bool:
        """这个靶点上我们的注入在不在。

        默认看标记段 —— 但写「键」的靶点（Codex 的 model_instructions_file）
        没有块可看，得由子类自己判。
        """
        return has_block(read_text(h.path))

    def body_path(self, h: Hit) -> Path:
        """人格正文实际落在哪个文件。

        多数靶点正文就写在靶点文件里；Codex 是「配置里一行指向另一个文件」，
        正文不在 config.toml 里。
        """
        return h.path

    # ---- 以下不用改 ----

    def inject(self, persona: str, dry: bool = False) -> Report:
        rep = Report(self.name, False, self.detect())
        if not rep.found:
            return rep
        hits = [h for h in self.collect()]
        rep.hits = hits
        if not hits:
            rep.note = "没有可写的靶点（版本不匹配或结构变了）"
            return rep
        if dry:
            rep.ok = True
            return rep

        bk = new_backup()
        body = self.body(persona)
        for h in hits:
            try:
                if h.how == "block":
                    old = read_text(h.path)
                    new = put_block(old, body, where="end")
                    if new == old:
                        rep.skipped.append(f"{h.path.name} 无需改动")
                        continue
                    bk.save(self.name, h.path)
                    write_like(h.path, new)
                    rep.changed.append(str(h.path))
                elif h.how == "policy":
                    old = read_text(h.path)
                    new = put_policy(old, body, self.clean_base(h))
                    if new == old:
                        rep.skipped.append(f"{h.path.name} 无需改动")
                        continue
                    bk.save(self.name, h.path)
                    write_like(h.path, new)
                    rep.changed.append(str(h.path))
                elif h.how == "text":
                    ok = self.apply_text(h, body, bk)
                    (rep.changed if ok else rep.skipped).append(
                        str(h.path) if ok else
                        f"{h.path.name}：{getattr(self, '_last_error', '') or '写入失败'}")
                elif h.how == "key":
                    ok = self.apply_key(h, persona, bk)
                    (rep.changed if ok else rep.skipped).append(
                        str(h.path) if ok else
                        f"{h.path.name}：{getattr(self, '_last_error', '') or '写入失败'}")
                else:
                    rep.skipped.append(f"{h.path.name} 未知方式 {h.how}")
            except OSError as e:
                rep.skipped.append(f"{h.path.name} 写失败：{e.strerror or e}")

        if bk.entries:
            bk.flush()
        else:
            # 全部「无需改动」时不落空目录 —— 备份列表里一堆 0 文件没法看
            try:
                bk.root.rmdir()
            except OSError:
                pass
        st = load_state()
        st.setdefault(self.name, {}).update({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backup": str(bk.root) if bk.entries else "",
            "files": len(rep.changed),
        })
        save_state(st)

        # 备份层出问题要让人看见，但不能算注入失败 —— 人格确实写进去了。
        # 只说一句，别把「注入成功」和「没留备份」混成同一个红叉。
        if bk.note:
            rep.note = f"{rep.note}｜{bk.note}" if rep.note else bk.note

        # 语义收紧：只要有一个靶点是真的写失败，整个目标就不算成功。
        # 之前只看「有没有一个 changed」，结果 3 个靶点里 1 个写进去、2 个
        # 权限不够，界面照样报「1/1 已破甲」—— 用户打开客户端发现人格没变，
        # 只会觉得这个工具坏了。
        failed = [s for s in rep.skipped if "无需改动" not in s]
        if failed:
            rep.ok = False
            if rep.changed:
                rep.note = (f"部分失败：{len(rep.changed)} 处已改，"
                            f"{len(failed)} 处没写成")
            elif len(failed) >= len(hits):
                rep.note = "全部靶点写入失败，看上面跳过原因"
            else:
                # 有靶点本来就不用改，只有剩下的那几个没写成 —— 别吓唬人
                rep.note = (f"{len(failed)}/{len(hits)} 个靶点没写成，"
                            f"其余本来就无需改动")
        else:
            rep.ok = True
        return rep

    def apply_text(self, h: Hit, body: str, bk: Backup) -> bool:
        return False

    def apply_key(self, h: Hit, persona: str, bk: Backup) -> bool:
        return False

    def revert(self) -> Report:
        """还原注入。

        主路径是**按标记段剥离**，不是拿备份写回。备份只是兜底，用来处理
        「文件在注入前不存在」这种要靠 exists=False 删掉的记录。

        为什么不信备份：`new_backup()` 每次注入都新建目录，而旧实现的
        `Backup.save()` 只在单个 Backup 实例内去重。第二次注入时目标文件已经
        是注入态，于是「注入后」被当成「原始」存了下来，revert 拿它写回就是
        原地覆盖 —— 界面上报「已还原 N 个」，打开客户端人格照旧。现在
        `_strip_for_backup()` 在落盘前剥了一层，历史备份里的脏数据仍然存在，
        所以剥离这条路必须独立成立。
        """
        rep = Report(self.name, False, self.detect())
        if not rep.found:
            rep.note = "未安装，跳过"
            return rep
        bk = find_target_backup(self.name)

        n = 0
        for h in self.collect():
            try:
                if not h.path.exists():
                    continue
                if not has_block(read_text(h.path)) and h.how != "key":
                    continue                    # 本来就没注入，别动
                if self.strip_target(h):
                    n += 1
                    rep.changed.append(str(h.path))
            except OSError as e:
                rep.skipped.append(f"{h.path.name} 还原失败：{e.strerror or e}")

        # 兜底：备份里记着「注入前不存在」的文件，剥完还剩空壳就删掉。
        # 不靠它判断主流程成没成 —— 剥不动的目标已经进 skipped 了。
        if bk:
            for path_str, meta in bk.entries.items():
                if meta.get("target") != self.name or meta.get("exists"):
                    continue
                p = Path(path_str)
                try:
                    if p.exists() and not read_text(p).strip():
                        p.unlink()
                        n += 1
                        rep.changed.append(str(p))
                except OSError as e:
                    rep.skipped.append(f"{p.name} 删除失败：{e.strerror or e}")

        rep.ok = not rep.skipped
        if rep.skipped:
            rep.note = (f"还原 {n} 个，{len(rep.skipped)} 个失败" if n
                        else "还原失败")
        else:
            rep.note = f"已还原 {n} 个文件" if n else "没有可还原的注入"
        return rep

    def strip_target(self, h: Hit) -> bool:
        """把单个靶点上的注入剥掉。返回是否真的改了文件。

        子类可以覆盖：Codex 的「注入」是一行键加一个正文文件，块标记只覆盖
        后者，键得单独删。

        写回时优先用备份里的**原始字节**，不用 `write_like()` ——
        注入统一写 UTF-8（客户端只认这个），用户原本的 GBK 文件因此变成了
        UTF-8。还原必须把它变回 GBK，否则等于我们把用户的文件编码永久改掉了。
        备份存的就是注入前的字节（已剥掉标记段），直接搬回去最准。

        拿不到原始字节时才退回「剥标记段 + 写 UTF-8」，功能仍然成立，
        只是文件编码停留在 UTF-8（可读，不会乱码）。
        """
        text = read_text(h.path)
        if not has_block(text):
            return False
        new = scrub_legacy(strip_block(text))
        if new == text:
            return False
        # 剥完只剩空白 = 这文件本来就是注入时新建的（zcode/dsh 的 AGENTS.md）。
        # 留一个空文件在用户目录里没意义，直接删。
        if not new.strip():
            h.path.unlink()
            return True

        # 1) 备份里有原始字节 → 原样写回（编码也还原）
        bk = find_target_backup(self.name)
        orig = bk.original_of(h.path) if bk else None
        if orig is not None:
            try:
                write_orig(h.path, orig)
                return True
            except OSError:
                pass        # 落不下去就退回路 2，别让还原整个失败
        # 2) 没有原始字节 → 剥完的内容写 UTF-8
        write_like(h.path, new)
        return True

    def status(self) -> str:
        if not self.detect():
            return "未安装"
        hits = self.collect()
        if not hits:
            return f"已安装（{self.root}），无可用靶点"
        live = sum(1 for h in hits if self.is_applied(h))
        return f"已安装（{self.root}），靶点 {len(hits)} 个，已注入 {live} 个"

    def verify(self, persona: str = "") -> list[dict]:
        """逐项体检。返回 [{ok, text, hint}]，给界面直接渲染。

        这一层只看磁盘：文件在不在、块在不在、写进去的是不是这份人格、
        结构还解析得了吗。模型到底照不照人格说话，得真发一条请求，那是另一件事。
        """
        out: list[dict] = []

        def add(ok: bool, text: str, hint: str = "") -> None:
            out.append({"ok": ok, "text": text, "hint": hint})

        if not self.detect():
            add(False, "未安装", "找不到安装目录")
            return out
        add(True, f"定位到 {self.root or '用户目录'}")

        hits = self.collect()
        if not hits:
            add(False, "没有可写的靶点", "版本不匹配或结构变了")
            return out

        mark = self.fingerprint_of(persona) if persona else ""
        for h in hits:
            name = _short(h.path)
            try:
                applied = self.is_applied(h)
            except Exception:                                  # noqa: BLE001
                applied = False
            if not applied:
                add(False, f"{name} 未注入", h.note or "")
                continue
            body = read_text(self.body_path(h))
            # 换行统一成 \n 再比。文件可能被编辑器存成 CRLF（Windows 上
            # 记事本、VS Code 默认就那样），而指纹是从内存里的字符串取的，
            # 里面只有 \n —— 不归一化的话，只要指纹跨行就永远匹配不上，
            # 体检报「注入的不是这份人格」，用户重跑多少次都一样。
            if mark and mark not in body.replace("\r\n", "\n"):
                add(False, f"{name} 注入的不是这份人格", "重跑一次破甲")
                continue
            left = residue(body)
            if left:
                add(False, f"{name} 里有旧工具残留：{'、'.join(left)}", "重跑一次破甲会清掉")
                continue
            add(True, f"{name} 就绪")
        return out
