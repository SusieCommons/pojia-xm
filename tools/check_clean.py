# -*- coding: utf-8 -*-
"""门禁：公开仓库里不该出现的东西，一个都不许有。

在推送之前跑一遍。CI 也跑同一套（.github/workflows/tests.yml），
但本地跑更快，且能查历史。

    python tools/check_clean.py            # 查工作区
    python tools/check_clean.py --history  # 连 git 历史一起查
"""
from __future__ import annotations

import argparse
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
    # 真实的本机绝对路径。带用户名的才算，`C:\\Users\\x` 这种示例放过。
    (r"C:\\\\Users\\\\[A-Za-z][A-Za-z0-9._-]{2,}", "本机绝对路径"),
    # 二维码 / 群图片：只拦真实引用，不拦「检测关键词」这种自指写法
    (r"<img[^>]+(qrcode|qr-code|group\.png)", "二维码图片标签"),
    (r"!\[[^\]]*\]\([^)]*(qrcode|qr-code|group\.png)", "二维码 Markdown 图片"),
]

# 已知的真实号码。写死在这里，改名换号时同步更新。
# 用具体值而不是「9 位以上数字」的正则 —— 后者会误伤占位值、
# 示例机器码、时间戳，最后只能靠加白名单掩盖，反而失去意义。
KNOWN_IDS = [
    "GROUP_ID_A",   # 引流群
    "GROUP_ID_B",   # 老工具引流群
    "ADMIN_ACCOUNT",  # 后台账号
]

# 这些文件本身在讨论「怎么拦」，不参与内容检查
SELF_EXEMPT = {
    "tools/check_clean.py",
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

        # 已知真实号码：逐行查，报行号
        for i, line in enumerate(t.splitlines(), 1):
            for kid in KNOWN_IDS:
                if kid in line:
                    bad.append("[号码] %s:%d 出现 %s" % (rel, i, kid))

    return bad


def check_history() -> list[str]:
    """查 git 历史里有没有被删掉的敏感文件。

    工作区干净不代表历史干净 —— 一个曾经提交过又被删掉的 secret.key
    还在 object 库里，clone 下来就能拿到。
    """
    bad = []
    try:
        p = subprocess.run(["git", "log", "--all", "--name-only", "--pretty=format:"],
                           cwd=ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return ["[历史] 读不到 git 历史：%s" % e]

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
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="check_clean")
    ap.add_argument("--history", action="store_true", help="连 git 历史一起查")
    a = ap.parse_args(argv[1:])

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
