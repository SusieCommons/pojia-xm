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
import os
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
    import re
    src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
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
        ck("codex 注入后含人格正文", "写安全工具的工程师" in txt)

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
        ck("zcode 含 begin 标记", "xm-persona:begin" in cur)
        ck("zcode 含 end 标记", "xm-persona:end" in cur)
        ck("zcode 标记只出现一次", cur.count("xm-persona:begin") == 1,
           "%d 次" % cur.count("xm-persona:begin"))

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
        ck("zcode 还原后无残留标记", "xm-persona:begin" not in back)
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
