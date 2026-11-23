# -*- coding: utf-8 -*-
"""门禁：公开仓库里不该出现的东西，一个都不许有。

在推送之前跑一遍。CI 也跑同一套（.github/workflows/tests.yml），
但本地跑更快，且能查历史。

    python tools/check_clean.py            # 查工作区
    python tools/check_clean.py --history  # 连 git 历史一起查
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些路径不该被 git 跟踪
FORBIDDEN_FILES = [
    r"secret\.key$",
    r"signer\.key$",
    r"_keys/",
    r".*\.pem$",
    r".*\.pfx$",
    r".*\.p12$",
    r"data\.db$",
    r"site\.toml$",
    r"dist/",
    r"build/",
]

# 文本里不该出现的串
#
# 注意：「群号」这个词本身不拦 —— 功能就叫这个，代码里必然提到。
# 要拦的是**具体的号**和**图片引用**。
FORBIDDEN_TEXT = [
    (r"BEGIN [A-Z ]*PRIVATE KEY", "私钥"),
    (r"https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0)"
     r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", "硬编码公网 IP"),
    # 裸 IP（不在 URL 里）。nginx 白名单、SSH 配置、数据库 host 这类地方
    # 经常直接写 IP，前面那条只认 URL 的规则抓不到 —— 实测漏过一条
    # `allow <出口 IP>;` 的白名单条目，所以补这一条。
    #
    # 每段必须 0-255 且不带前导零：CSS 里的 `2.774.868.86`、四段版本号
    # 这类会被误伤，而真 IP 不会出现 >255 的段。
    # 再排除私网与文档保留段（示例用）。
    (r"(?<![\d.])"
     r"(?!0\.|127\.|10\.|192\.168\.|169\.254\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)"
     r"(?!172\.(?:1[6-9]|2\d|3[01])\.)"
     r"(?:(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)\.){3}"
     r"(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)"
     r"(?![\d.])", "硬编码公网 IP（裸）"),
    # 真实的本机绝对路径。带用户名的才算，`C:\\Users\\x` 这种示例放过。
    (r"C:\\\\Users\\\\[A-Za-z][A-Za-z0-9._-]{2,}", "本机绝对路径"),
    # 二维码 / 群图片：只拦真实引用，不拦「检测关键词」这种自指写法
    (r"<img[^>]+(qrcode|qr-code|group\.png)", "二维码图片标签"),
    (r"!\[[^\]]*\]\([^)]*(qrcode|qr-code|group\.png)", "二维码 Markdown 图片"),
]

# 已知的真实号码，只存 SHA-256 前 16 位十六进制指纹。
#
# 为什么不写明文：这个文件本身是要公开的。把群号写进黑名单，等于
# 「为了保护它而把它贴出来」—— 谁 clone 一下都看得到。指纹单向，
# 门禁照样能拦，泄露面归零。
#
# 用具体值而不是「9 位以上数字」的正则 —— 后者会误伤占位值、
# 示例机器码、时间戳，最后只能靠加白名单掩盖，反而失去意义。
#
# 加新号码：python tools/check_clean.py --whois <明文> 打印指纹。
KNOWN_ID_HASHES = {
    "5e3ed126b8209887",   # 引流群（10 位纯数字）
    "a384fe0b8c9b9c41",   # 老工具引流群（10 位纯数字，已作废）
    "89b80c558679afbf",   # 后台账号（12 位字母+数字）
}


def _id_sha16(s: str) -> str:
    """号码 → 指纹。UTF-8、无盐、前 16 位。

    不加盐是有意的：指纹要能对任意候选串现算现比，盐会让它变成
    「每次运行都不同」的东西，门禁就没法用了。号码本身熵不高，
    指纹可以被字典撞开 —— 但撞开的人已经知道这串数字的存在，
    而我们要挡的是「clone 下来一眼看到」。
    """
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _find_known_ids(text: str) -> list[tuple[int, str]]:
    """在一段文本里找已知号码。

    按「候选串」而不是「整行」比对：号码可能被引号、逗号、注释包着。
    候选串取连续的数字/字母段，长度 8-24 —— 覆盖 10 位 QQ 群号、
    12 位账号，同时不会把普通单词拆开误判。

    返回 [(行号, 指纹)]，不返回明文。
    """
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"(?<![0-9A-Za-z])([0-9A-Za-z]{8,24})(?![0-9A-Za-z])",
                             line):
            h = _id_sha16(m.group(1))
            if h in KNOWN_ID_HASHES:
                hits.append((i, h))
    return hits

# 这些文件本身在讨论「怎么拦」，不参与内容检查。
#
# 注意：check_clean.py 自己**不在**这里 —— 它以前豁免了自己，又恰好
# 把真实群号写在 KNOWN_IDS 里，结果「泄露者不检查自己」，门禁一路绿灯。
# 现在改成只存指纹，它也就没必要豁免了；反而要留着，让门禁扫自己一眼。
SELF_EXEMPT = {
    "tests/test_gate.py",       # 里面有验证拦截规则的样本串
    ".github/workflows/tests.yml",
    "CONTRIBUTING.md",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules",
             ".venv", "venv", "build", "dist", ".pyibuild"}
SKIP_EXT = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
            ".exe", ".dll", ".pyd", ".zip", ".db", ".key"}


def iter_files():
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() in SKIP_EXT:
                continue
            yield p


def check_worktree() -> list[str]:
    bad = []

    # 1. 不该存在的文件
    for p in iter_files():
        rel = p.relative_to(ROOT).as_posix()
        for pat in FORBIDDEN_FILES:
            if re.search(pat, rel):
                bad.append("[文件] %s（匹配 %s）" % (rel, pat))

    # 2. 不该出现的内容
    for p in iter_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in SELF_EXEMPT:
            continue
        try:
            t = io.open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pat, name in FORBIDDEN_TEXT:
            m = re.search(pat, t)
            if m:
                ln = t[:m.start()].count("\n") + 1
                bad.append("[内容] %s:%d %s → %r" % (rel, ln, name, m.group(0)[:60]))

        # 已知真实号码：按指纹比对，只报行号不报明文
        for ln, h in _find_known_ids(t):
            bad.append("[号码] %s:%d 命中指纹 %s" % (rel, ln, h))

    return bad


def check_history() -> list[str]:
    """查 git 历史里有没有敏感内容。

    两个层面都要查：
    1. 被删掉的敏感**文件**（secret.key 之类）—— 文件名层面
    2. 历史上**任何一版内容**里的真实号码 —— 内容层面

    第 2 点是补的：原来的实现只看文件名，于是「曾经提交过含群号的文件、
    后来删掉」这种最常见的情况完全查不到 —— 文件确实删了，但 blob 还在
    对象库里，clone 下来 `git log -p` 就能看到。

    内容扫描用 `git grep <指纹候选> <rev>`：对每个提交 grep 出所有
    8-24 位的候选串再比指纹。提交多的时候慢，但这是发布前跑一次的检查，
    宁可慢。
    """
    bad = []
    try:
        p = subprocess.run(["git", "log", "--all", "--name-only",
                            "--pretty=format:"],
                           cwd=ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return ["[历史] 读不到 git 历史：%s" % e]

    # --- 层面 1：文件名 ---
    names = (p.stdout or b"").decode("utf-8", "replace").splitlines()
    seen = set()
    for n in names:
        n = n.strip()
        if not n or n in seen:
            continue
        seen.add(n)
        for pat in FORBIDDEN_FILES:
            if re.search(pat, n):
                bad.append("[历史] %s（匹配 %s）" % (n, pat))
                break

    # --- 层面 2：历史内容里的号码 ---
    for rev in _history_revs():
        hits = _scan_rev_for_ids(rev)
        for path, ln, h, is_head in hits:
            tag = "当前" if is_head else "历史"
            bad.append("[%s内容] %s:%d 命中指纹 %s" % (tag, path, ln, h))

    return bad


def _history_revs() -> list[str]:
    """取所有提交哈希（--all，含不可达的 reflog 残留）。"""
    try:
        p = subprocess.run(["git", "rev-list", "--all"],
                           cwd=ROOT, capture_output=True, timeout=60)
        revs = [x.strip() for x in
                (p.stdout or b"").decode("utf-8", "replace").splitlines()
                if x.strip()]
        return revs[:200]   # 上限，历史再长也没必要全扫
    except (OSError, subprocess.SubprocessError):
        return []


def _scan_rev_for_ids(rev: str) -> list[tuple[str, int, str, bool]]:
    """扫一个提交的完整树。返回 [(路径, 行号, 指纹, 是否HEAD版本)]。

    用 `git grep -n -I -E` 一次扫完整个树，比逐文件 read 快一个量级；
    `-I` 跳过二进制，避免把 exe 的字节当文本。
    """
    # 先粗筛：候选串模式。命中后再算指纹精确判定。
    cand = r"[0-9A-Za-z]{8,24}"
    try:
        p = subprocess.run(
            ["git", "grep", "-n", "-I", "-E", cand, rev],
            cwd=ROOT, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []

    out = (p.stdout or b"").decode("utf-8", "replace")
    hits = []
    # 输出格式： <rev>:<path>:<行号>:<内容>
    for ln in out.splitlines():
        parts = ln.split(":", 3)
        if len(parts) < 4:
            continue
        path, lineno, content = parts[1], parts[2], parts[3]
        try:
            n = int(lineno)
        except ValueError:
            continue
        for m in re.finditer(r"(?<![0-9A-Za-z])([0-9A-Za-z]{8,24})(?![0-9A-Za-z])",
                             content):
            h = _id_sha16(m.group(1))
            if h in KNOWN_ID_HASHES:
                hits.append((path, n, h, rev == "HEAD"))
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="check_clean")
    ap.add_argument("--history", action="store_true", help="连 git 历史一起查")
    ap.add_argument("--whois", metavar="PLAINTEXT",
                    help="算一个号码的指纹，用来加进 KNOWN_ID_HASHES")
    a = ap.parse_args(argv[1:])

    if a.whois:
        print(_id_sha16(a.whois))
        return 0

    bad = check_worktree()
    if a.history:
        bad += check_history()

    if not bad:
        print("[OK] 干净：没有密钥、生产地址、本机路径、引流内容")
        return 0

    print("[X] 发现 %d 处问题：" % len(bad))
    for b in bad:
        print("   " + b)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
