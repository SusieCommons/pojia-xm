# -*- coding: utf-8 -*-
"""发布前校验：对 dist/ 里的 exe 做一轮可复现的检查。

分两层：

  **静态**（不需要运行 exe）
    * 体积、SHA-256
    * PE 版本资源（FileVersion / ProductVersion 必须是 pyproject 里那个版本）
    * 内嵌图标的帧数

  **动态**（在临时目录里跑真 exe）
    * `--version` 输出对得上
    * 条款闸门：未同意时退出码 2
    * codex 注入 → 校验 → 还原，还原后逐字节等于原始 GBK
    * zcode 追加模式：标记段只出现一次，原文保留，还原后无残留
    * 连注入三次体积不变（幂等）
    * 无 BOM 的 UTF-16LE 目标：注入后原文可读，还原后逐字节一致

隔离方式：HOME / USERPROFILE / XM_DATA / CODEX_HOME / ZCODE_HOME / DSH_HOME
全部指到临时目录，绝不碰本机真实客户端配置。

用法：
    python tools/verify_release.py                      # 自动找 dist 里的 exe
    python tools/verify_release.py dist/xxx.exe         # 指定文件
    python tools/verify_release.py --static-only        # 只跑静态检查
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL: list[str] = []
WARN: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    print(("[ok]   " if cond else "[FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def warn(name: str, detail: str = "") -> None:
    print("[warn] " + name + (("  " + detail) if detail else ""))
    WARN.append(name)


# ---------------------------------------------------------------- 静态检查

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _Fixed(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wt.DWORD), ("dwStrucVersion", wt.DWORD),
        ("dwFileVersionMS", wt.DWORD), ("dwFileVersionLS", wt.DWORD),
        ("dwProductVersionMS", wt.DWORD), ("dwProductVersionLS", wt.DWORD),
        ("dwFileFlagsMask", wt.DWORD), ("dwFileFlags", wt.DWORD),
        ("dwFileOS", wt.DWORD), ("dwFileType", wt.DWORD),
        ("dwFileSubtype", wt.DWORD), ("dwFileDateMS", wt.DWORD),
        ("dwFileDateLS", wt.DWORD),
    ]


def read_version_info(path: str) -> dict:
    """读 PE 版本资源。纯 ctypes，不引入 pefile。

    VerQueryValueW 的最后一个参数是 PUINT，返回的是指针长度；直接用
    int() 会拿到 c_ulong 对象而不是数值 —— 必须显式 .value。
    """
    ver = ctypes.WinDLL("version")
    ver.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = wt.DWORD
    ver.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    ver.GetFileVersionInfoW.restype = wt.BOOL
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wt.UINT)]
    ver.VerQueryValueW.restype = wt.BOOL

    size = int(ver.GetFileVersionInfoSizeW(path, None))
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return {}

    def query(sub):
        ptr, n = ctypes.c_void_p(), wt.UINT()
        if not ver.VerQueryValueW(buf, sub, ctypes.byref(ptr), ctypes.byref(n)):
            return None, 0
        return ptr, n.value

    out: dict = {}
    ptr, _ = query("\\")
    if ptr:
        fx = ctypes.cast(ptr, ctypes.POINTER(_Fixed)).contents
        if fx.dwSignature == 0xFEEF04BD:
            out["filevers"] = "%d.%d.%d.%d" % (
                fx.dwFileVersionMS >> 16, fx.dwFileVersionMS & 0xFFFF,
                fx.dwFileVersionLS >> 16, fx.dwFileVersionLS & 0xFFFF)
            out["prodvers"] = "%d.%d.%d.%d" % (
                fx.dwProductVersionMS >> 16, fx.dwProductVersionMS & 0xFFFF,
                fx.dwProductVersionLS >> 16, fx.dwProductVersionLS & 0xFFFF)

    langs = []
    ptr, n = query("\\VarFileInfo\\Translation")
    if ptr and n >= 4:
        cnt = n // 4
        arr = ctypes.cast(ptr, ctypes.POINTER(wt.WORD * (2 * cnt))).contents
        langs = [(arr[i * 2], arr[i * 2 + 1]) for i in range(cnt)]
    if not langs:
        langs = [(0x0409, 0x04B0), (0x0804, 0x04B0)]

    for lang, cp in langs:
        for key in ("FileVersion", "ProductVersion", "FileDescription",
                    "ProductName", "CompanyName", "OriginalFilename", "InternalName"):
            if key in out:
                continue
            ptr, _ = query("\\StringFileInfo\\%04x%04x\\%s" % (lang, cp, key))
            if ptr:
                out[key] = ctypes.wstring_at(ptr)
    return out


def ico_frames(path: str) -> int:
    with open(path, "rb") as f:
        head = f.read(6)
    if len(head) < 6:
        return 0
    res, typ, cnt = struct.unpack("<HHH", head)
    return cnt if (res == 0 and typ == 1) else 0


def project_version() -> str:
    src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 内嵌资源

#: exe 里被打进去的数据文件。datas 里的条目在归档里是明文，能直接取。
_EMBED_UI = "ui/index.html"
_EMBED_PERSONA = "persona.md"

#: UI 源码里的占位群号。打出来的包里绝不该看见它。
_UI_PLACEHOLDER = "100000000"

_RE_GRP_B = re.compile(r'id="grpNo">(\d{5,12})<')


def extract_embedded(exe: str, suffix: str) -> bytes | None:
    """从 PyInstaller 单文件 exe 里取一个内嵌数据文件。

    suffix 用 / 分隔的尾部匹配（归档里用反斜杠，两种都归一）。取不到就
    返回 None —— 调用方据此判「这版没打进去」而不是崩掉。

    PyInstaller 只在构建环境装，发布校验本来就在那个环境跑；真没有就返回
    None，由调用方降级成 warn，不至于因为缺个可选依赖把校验整体打挂。
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:                                          # noqa: BLE001
        return None
    want = suffix.replace("\\", "/").lstrip("/")
    try:
        a = CArchiveReader(exe)
    except Exception:                                          # noqa: BLE001
        return None
    try:
        for name in a.toc:
            if name.replace("\\", "/").endswith(want):
                return a.extract(name)
    except Exception:                                          # noqa: BLE001
        return None
    return None


def ui_group_no(html: bytes) -> str:
    """取前端首屏那份群号。优先 <b id="grpNo">，没有再退回 BRAND_MOCK。

    首屏静态值最要紧：后端 brand 接口回来之前（或者接口一直失败时），
    用户看到的就是它。
    """
    s = html.decode("utf-8", "replace")
    m = _RE_GRP_B.search(s)
    if m:
        return m.group(1)
    m = re.search(r'BRAND_MOCK\s*=\s*\{[^}]*group\s*:\s*"(\d{5,12})"', s)
    return m.group(1) if m else ""


def persona_group_no(md: bytes) -> str:
    """取人格正文里那份群号。

    人格是注入到别人客户端里的纯文本，它跟程序分离，只能写死 —— 所以它
    是这里唯一能拿来跟 UI 对照的独立真值来源。
    """
    s = md.decode("utf-8", "replace")
    m = re.search(r"小熊猫交流群\s*(\d{5,12})", s)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{5,12})(?!\d)", s[-(4000):])
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 动态检查

def looks_utf16(raw: bytes) -> bool:
    """与 core/base.py 的判据保持一致。

    不能靠「decode 成功」判断 —— 任何偶数长度字节都能被 latin1 收下，
    而 UTF-16LE 的 ASCII 部分在 UTF-8 下也是合法字节，只是解出带 NUL 的垃圾。
    """
    if len(raw) < 2 or len(raw) % 2:
        return False
    zeros = raw.count(0)
    ratio = zeros / float(len(raw))
    if not (0.08 <= ratio <= 0.5):
        return False
    even = sum(1 for i in range(0, len(raw), 2) if raw[i] == 0)
    odd = sum(1 for i in range(1, len(raw), 2) if raw[i] == 0)
    return bool(zeros) and max(even, odd) / float(zeros) >= 0.85


def make_env(tmp: str) -> dict:
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "XM_DATA": os.path.join(tmp, "data"),
        "HOME": home,
        "USERPROFILE": home,
        "CODEX_HOME": os.path.join(home, ".codex"),
        "ZCODE_HOME": os.path.join(home, ".zcode"),
        "DSH_HOME": os.path.join(home, ".dsh"),
    })
    drive, rest = os.path.splitdrive(home)
    env["HOMEDRIVE"], env["HOMEPATH"] = drive, rest
    return env


def run_exe(exe: str, env: dict, *args: str) -> tuple[int, str, str]:
    r = subprocess.run([exe, *args], capture_output=True, env=env, timeout=180)
    return (r.returncode,
            r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def seed_codex(home: str) -> tuple[str, bytes]:
    codex = os.path.join(home, ".codex")
    os.makedirs(codex, exist_ok=True)
    persona = os.path.join(codex, "xm-persona.md")
    io.open(os.path.join(codex, "config.toml"), "w",
            encoding="utf-8", newline="\n").write(
        'model = "gpt-5"\n'
        'model_instructions_file = "%s"\n' % persona.replace("\\", "/"))
    gbk = "# 我的人格\n保持简洁。\n".encode("gbk")
    with open(persona, "wb") as f:
        f.write(gbk)
    return persona, gbk


def seed_zcode(home: str, text: str = "# 原文\n\n保留这一行。\n") -> tuple[str, str]:
    d = os.path.join(home, ".zcode")
    os.makedirs(d, exist_ok=True)
    md = os.path.join(d, "AGENTS.md")
    io.open(md, "w", encoding="utf-8", newline="\n").write(text)
    return md, text


def dynamic(exe: str) -> None:
    print("\n--- 动态：条款闸门 ---")
    tmp = tempfile.mkdtemp(prefix="xmrel_gate_")
    try:
        env = make_env(tmp)
        rc, out, _ = run_exe(exe, env, "dir")
        ck("未同意条款时 dir 被拦住（rc=2）", rc == 2, "rc=%d" % rc)
        ck("拦截时打印了条款正文", "使用前须知" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n--- 动态：codex 注入 / 校验 / 还原 ---")
    tmp = tempfile.mkdtemp(prefix="xmrel_codex_")
    try:
        env = make_env(tmp)
        persona, gbk = seed_codex(env["HOME"])

        rc, out, _ = run_exe(exe, env, "apply", "--accept", "-t", "codex")
        ck("codex apply 退出码 0", rc == 0, "rc=%d" % rc)

        raw = open(persona, "rb").read()
        ck("codex 注入后体积合理（>5 KB）", len(raw) > 5000, "%d B" % len(raw))
        ck("codex 注入后无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
        ck("codex 注入后不是 UTF-16 形态", not looks_utf16(raw))
        try:
            txt = raw.decode("utf-8")
            ok = True
        except UnicodeDecodeError as e:
            txt, ok = "", False
            print("  UTF-8 解码失败: %s" % e)
        ck("codex 注入后 UTF-8 可解码", ok)
        ck("codex 注入后含人格正文", "小熊猫是一个本地客户端" in txt)

        rc, out, _ = run_exe(exe, env, "verify", "--accept", "-t", "codex")
        ck("codex verify 退出码 0", rc == 0, "rc=%d" % rc)

        rc, _, _ = run_exe(exe, env, "revert", "--accept", "-t", "codex")
        ck("codex revert 退出码 0", rc == 0, "rc=%d" % rc)
        back = open(persona, "rb").read()
        ck("codex 还原逐字节等于原始 GBK", back == gbk,
           "原 %d / 现 %d" % (len(gbk), len(back)))

        rc, out, _ = run_exe(exe, env, "verify", "--accept", "-t", "codex")
        ck("还原后 verify 报未注入（rc!=0）", rc != 0, "rc=%d" % rc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n--- 动态：zcode 追加模式 ---")
    tmp = tempfile.mkdtemp(prefix="xmrel_zcode_")
    try:
        env = make_env(tmp)
        md, orig = seed_zcode(env["HOME"])

        rc, _, _ = run_exe(exe, env, "apply", "--accept", "-t", "zcode")
        ck("zcode apply 退出码 0", rc == 0, "rc=%d" % rc)
        cur = io.open(md, encoding="utf-8", newline="").read()
        ck("zcode 保留用户原文", "保留这一行" in cur)
        ck("zcode 有封口行", "xm-doc:seal" in cur)
        ck("zcode 封口行只出现一次", cur.count("xm-doc:seal") == 1,
           "%d 次" % cur.count("xm-doc:seal"))
        # 1.5.0：正文之前不能有任何标记行 —— 实测会让模型把整段判成
        # 「内部配置」（34 字拒绝）或直接被上游过滤（0 字）。
        ck("zcode 无正文前置标记", "xm-doc:begin" not in cur)
        # 用户原文必须原封不动在文件开头。断言取 seed 的第一行，不写死文案 ——
        # 之前这里写死了另一份 fixture 的字符串，明明行为正确也报 FAIL。
        first = orig.splitlines()[0]
        ck("zcode 从原文第一行开始", cur.startswith(first), repr(cur[:20]))
        ck("zcode 封口在正文之后", cur.find(first) < cur.find("xm-doc:seal"))
        # 1.4.1 起块外不再有英文 notice。它留在文件里的话，模型在 reasoning
        # 里会把它逐条列成越狱特征，人格等于白注入。
        for bad in ("[configuration]", "not a role-play request",
                    "do not evaluate", "needs your approval"):
            ck("zcode 无退场 notice：%s" % bad, bad not in cur)

        # 幂等：连注入三次体积不变
        sizes = [len(cur.encode("utf-8"))]
        for _ in range(2):
            run_exe(exe, env, "apply", "--accept", "-t", "zcode")
            sizes.append(len(io.open(md, encoding="utf-8").read().encode("utf-8")))
        ck("连注入三次体积不变（幂等）", len(set(sizes)) == 1, str(sizes))

        rc, _, _ = run_exe(exe, env, "revert", "--accept", "-t", "zcode")
        ck("zcode revert 退出码 0", rc == 0, "rc=%d" % rc)
        back = io.open(md, encoding="utf-8", newline="").read()
        ck("zcode 还原等于原文", back == orig,
           "原 %r / 现 %r" % (orig[:16], back[:16]))
        ck("zcode 还原后无残留标记", "xm-doc:seal" not in back)
        ck("zcode 还原后无旧标记", "xm-persona:begin" not in back)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n--- 动态：无 BOM 的 UTF-16LE 目标 ---")
    tmp = tempfile.mkdtemp(prefix="xmrel_u16_")
    try:
        env = make_env(tmp)
        d = os.path.join(env["HOME"], ".zcode")
        os.makedirs(d, exist_ok=True)
        md = os.path.join(d, "AGENTS.md")
        u16 = "# 我的配置\r\n保留这一行。\r\n".encode("utf-16-le")
        with open(md, "wb") as f:
            f.write(u16)

        rc, _, _ = run_exe(exe, env, "apply", "--accept", "-t", "zcode")
        ck("UTF-16 目标 apply 退出码 0", rc == 0, "rc=%d" % rc)
        after = open(md, "rb").read()
        ck("UTF-16 目标注入后按 UTF-8 可读", _utf8_ok(after),
           "%d B" % len(after))

        run_exe(exe, env, "revert", "--accept", "-t", "zcode")
        back = open(md, "rb").read()
        ck("UTF-16LE 还原逐字节一致", back == u16,
           "原 %d / 现 %d" % (len(u16), len(back)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    print("\n--- 动态：还原要真的回到官方配置 ---")
    # 1.5.4 的三个现场，逐个用真 exe 复现。它们的共同症状是：命令退出码 0、
    # 界面报「已还原 N 个文件」，但客户端里人格还在。
    tmp = tempfile.mkdtemp(prefix="xmrel_rev_")
    try:
        env = make_env(tmp)
        codex_dir = os.path.join(env["HOME"], ".codex")
        os.makedirs(codex_dir, exist_ok=True)
        cfg = os.path.join(codex_dir, "config.toml")
        persona = os.path.join(codex_dir, "xm-persona.md")
        io.open(cfg, "w", encoding="utf-8", newline="\n").write('model = "gpt-5"\n')

        # (a) 注入前没有 xm-persona.md → 还原后必须不存在。
        #     旧实现里这一步靠「备份记 exists=False」，而现在人格文件里是整篇
        #     正文（不是空壳），只要兜底逻辑判「空才删」就漏。
        rc, _, _ = run_exe(exe, env, "apply", "--accept", "-t", "codex")
        ck("(a) apply 退出码 0", rc == 0, "rc=%d" % rc)
        ck("(a) 注入后人格文件已建", os.path.isfile(persona))
        rc, _, _ = run_exe(exe, env, "revert", "--accept", "-t", "codex")
        ck("(a) revert 退出码 0", rc == 0, "rc=%d" % rc)
        ck("(a) 还原后人格文件已删除", not os.path.exists(persona),
           "残留 %d B" % (os.path.getsize(persona) if os.path.exists(persona) else 0))
        ck("(a) 还原后配置无 model_instructions_file",
           "model_instructions_file" not in io.open(cfg, encoding="utf-8").read())

        # (b) 脏备份：备份里存的就是我们的人格正文（真机历史备份全这样）。
        #     还原不能被它拦住 —— 那是「注入后」，不是「注入前」。
        bdir = os.path.join(env["XM_DATA"], "backups", "20260101-000000")
        os.makedirs(os.path.join(bdir, "files"), exist_ok=True)
        dirty = ("# 小熊猫\n\n小熊猫是一个本地客户端，面向安全工程与研发运维场景。\n")
        with open(os.path.join(bdir, "files", "00_xm-persona.md"), "wb") as f:
            f.write(dirty.encode("utf-8"))
        with open(persona, "wb") as f:
            f.write(dirty.encode("utf-8"))
        with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({persona: {"exists": True, "target": "codex",
                                 "backup": "files/00_xm-persona.md",
                                 "sha": hashlib.sha256(dirty.encode("utf-8")).hexdigest(),
                                 "size": len(dirty.encode("utf-8"))}},
                      f, ensure_ascii=False)
        io.open(cfg, "a", encoding="utf-8", newline="\n").write(
            'model_instructions_file = "%s"\n' % persona.replace("\\", "\\\\"))
        rc, _, _ = run_exe(exe, env, "revert", "--accept", "-t", "codex")
        ck("(b) 脏备份场景 revert 退出码 0", rc == 0, "rc=%d" % rc)
        ck("(b) 脏备份没被当成用户原文写回", not os.path.exists(persona),
           "残留 %d B" % (os.path.getsize(persona) if os.path.exists(persona) else 0))

        # (c) 文件被编辑器整篇转成 CRLF → 封口行 CRC 对不上，仍要能剥干净。
        zdir = os.path.join(env["HOME"], ".zcode")
        os.makedirs(zdir, exist_ok=True)
        zmd = os.path.join(zdir, "AGENTS.md")
        io.open(zmd, "w", encoding="utf-8", newline="\n").write("# 原文\n\n保留这一行。\n")
        run_exe(exe, env, "apply", "--accept", "-t", "zcode")
        raw = open(zmd, "rb").read()
        with open(zmd, "wb") as f:              # 整篇转 CRLF，等同记事本存一次
            f.write(raw.replace(b"\n", b"\r\n"))
        run_exe(exe, env, "revert", "--accept", "-t", "zcode")
        back = open(zmd, "rb").read()
        ck("(c) CRLF 转换后仍能剥干净", b"xm-doc:seal" not in back)
        ck("(c) CRLF 转换后无正文残留", "小熊猫是一个本地客户端" not in
           back.decode("utf-8", "replace"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _utf8_ok(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="发布前校验")
    ap.add_argument("exe", nargs="?", help="exe 路径，默认取 dist 里最新的")
    ap.add_argument("--static-only", action="store_true", help="只跑静态检查")
    a = ap.parse_args()

    exe = a.exe
    if not exe:
        cands = glob.glob(os.path.join(ROOT, "dist", "*.exe"))
        if not cands:
            print("[FAIL] dist/ 里没有 exe，先构建")
            return 1
        exe = max(cands, key=os.path.getmtime)
    exe = os.path.abspath(exe)
    if not os.path.exists(exe):
        print("[FAIL] 文件不存在:", exe)
        return 1

    want = project_version()
    print("exe    :", exe)
    print("体积   : %,d B".replace(",", "") % os.path.getsize(exe))
    print("sha256 :", sha256_of(exe))
    print("期望版本:", want)
    print("-" * 62)

    print("--- 静态：PE 版本资源 ---")
    vi = read_version_info(exe)
    if not vi:
        # 打包时 build/version_info.txt 不在，PyInstaller 就不写 VS_VERSIONINFO。
        # CI 里这是常态（build/ 在 .gitignore 里），所以判据不是"有没有资源"，
        # 而是"资源里的版本对不对" —— 没资源就说明打包前没生成，指出来即可。
        ck("exe 带版本资源", False,
           "GetFileVersionInfo 返回空；打包前先跑 tools/make_version_info.py")
    else:
        fv = vi.get("FileVersion", "")
        ck("FileVersion 与源码一致", fv == want, "exe=%r want=%r" % (fv, want))
        pv = vi.get("ProductVersion", "")
        ck("ProductVersion 与源码一致", pv == want, "exe=%r want=%r" % (pv, want))
        for k in ("FileDescription", "ProductName", "OriginalFilename", "InternalName"):
            if k in vi:
                print("  %-16s %s" % (k, vi[k]))

    print("\n--- 静态：图标 ---")
    ico = os.path.join(ROOT, "build", "icon.ico")
    if os.path.exists(ico):
        n = ico_frames(ico)
        print("  build/icon.ico  %d B  %d 帧" % (os.path.getsize(ico), n))
        ck("图标是合法 ICO", n > 0)
        if n and n < 7:
            warn("图标帧数偏少", "%d 帧，高分屏可能糊" % n)
    else:
        # build/ 被 .gitignore 排除，CI 里必然没有 —— 这不是缺陷。
        warn("build/icon.ico 不存在", "打包时用了默认图标（CI 属正常）")

    print("\n--- 静态：内嵌前端与人格的群号 ---")
    ui_raw = extract_embedded(exe, _EMBED_UI)
    md_raw = extract_embedded(exe, _EMBED_PERSONA)
    if ui_raw is None:
        warn("取不到内嵌 %s" % _EMBED_UI,
             "没装 PyInstaller 或归档结构变了，这项跳过")
    else:
        ug = ui_group_no(ui_raw)
        ck("内嵌 UI 能解析出群号", bool(ug), "got=%r" % ug)
        ck("内嵌 UI 不是占位群号 %s" % _UI_PLACEHOLDER,
           ug != _UI_PLACEHOLDER,
           "打包前先跑 tools/make_brand.py --group <真号>；"
           "UI 有 6 处硬编码，漏注入就会显示占位号")
        # 前端任意位置还残留占位号都算错：BRAND_MOCK / 兜底分支同样会被
        # 用户看到（接口超时时走的就是兜底）。
        left = ui_raw.count(_UI_PLACEHOLDER.encode())
        ck("内嵌 UI 全文无占位群号残留", left == 0, "%d 处" % left)

        if md_raw is not None:
            pg = persona_group_no(md_raw)
            ck("内嵌人格能解析出群号", bool(pg), "got=%r" % pg)
            # 两份都写死群号，一定会有「改了 A 忘了 B」。人格是对外的真值：
            # 它注入到别人机器里之后就改不动了，界面必须跟它一致。
            ck("内嵌 UI 群号与人格一致", bool(ug) and ug == pg,
               "ui=%r persona=%r" % (ug, pg))
        else:
            warn("取不到内嵌 %s，群号一致性未校验" % _EMBED_PERSONA)

    if not a.static_only:
        print("\n--- 动态：--version ---")
        tmp = tempfile.mkdtemp(prefix="xmrel_ver_")
        try:
            rc, out, _ = run_exe(exe, make_env(tmp), "--version")
            got = out.strip()
            ck("--version 退出码 0", rc == 0, "rc=%d" % rc)
            ck("--version 输出等于源码版本", got == want, "got=%r want=%r" % (got, want))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        dynamic(exe)

    print("-" * 62)
    if WARN:
        print("警告 %d 项：" % len(WARN))
        for w in WARN:
            print("  -", w)
    print("失败 %d 项" % len(FAIL))
    for f in FAIL:
        print("  -", f)
    if not FAIL:
        print("\n结论：可以发布")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
