# -*- coding: utf-8 -*-
"""把真实群号注入打包副本 —— 源码仓里永远只有占位值。

为什么要做成「打包时注入」而不是直接改源码
----------------------------------------
`tools/check_clean.py` 里有一份 `KNOWN_ID_HASHES`，存的是真实群号的
SHA-256 前 16 位。它扫工作区和全部历史，命中就报错、CI 直接红。

也就是说：**真实群号一旦写进 `core/brand.py`，这个仓库就再也推不上去了**。
但 exe 里必须有真号，否则用户拿到的是「交流群 100000000」这种占位卡片，
加不进任何群。

两条约束只能这样同时满足：源码保持占位，打包时替换成真值。生成的副本
写在 `build/`（已在 .gitignore 里），不碰被跟踪的文件。

用法
----
    python tools/make_brand.py --group <群号>
    python tools/make_brand.py --group <群号> --name <群名>
    python tools/make_brand.py --check build/brand_local/core/brand.py

产物
----
    build/brand_local/            注入后的整份源码树（只有品牌常量不同）
    build/brand_local/core/brand.py

打包时把 `build/brand_local` 作为工作目录，或让 spec 从这里取
`core/brand.py`。两种都行，见 xiaoxiongmao.spec 里的说明。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "brand_local"

#: 要被替换的常量。顺序固定，替换时逐条校验「恰好命中一次」。
FIELDS = ("DEFAULT_GROUP_NO", "DEFAULT_GROUP_NAME", "DEFAULT_TIP")

#: 占位值。源码里必须是这个，否则说明有人直接改了源码 —— 那是要被门禁拦的。
PLACEHOLDER_NO = "100000000"

#: 复制到副本里的东西。其余不复制：dist/build/__pycache__ 都不该进副本。
COPY = [
    "app.py",
    "xm.py",
    "persona.md",
    "requirements.txt",
    "xiaoxiongmao.spec",
    "core",
    "ui",
    "tests",
    "tools",
]

_RE_NO = re.compile(r"^\d{5,12}$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def _sub_const(text: str, name: str, value: str) -> tuple[str, str]:
    """把一个 `NAME = "..."` 常量换成新值。返回 (新文本, 错误)。"""
    pat = re.compile(r'^(%s\s*=\s*)("(?:[^"\\]|\\.)*")' % re.escape(name),
                     re.MULTILINE)
    hits = list(pat.finditer(text))
    if len(hits) != 1:
        return text, "%s 命中 %d 次（应恰好 1 次）" % (name, len(hits))
    # 值里有反斜杠或引号就拒绝 —— 品牌文案不该需要转义，出现说明输入有问题
    if '"' in value or "\\" in value:
        return text, "%s 的值不能含引号或反斜杠：%r" % (name, value)
    new = pat.sub(lambda m: m.group(1) + '"' + value + '"', text, count=1)
    return new, ""


def inject_text(text: str, group: str, name: str, tip: str) -> tuple[str, list[str]]:
    """内存里做一次注入。返回 (新文本, 错误列表)。"""
    errs: list[str] = []
    for field, value in (("DEFAULT_GROUP_NO", group),
                         ("DEFAULT_GROUP_NAME", name),
                         ("DEFAULT_TIP", tip)):
        if not value:
            continue                      # 空 = 不改这一项
        text, err = _sub_const(text, field, value)
        if err:
            errs.append(err)
    return text, errs


def build(group: str, name: str, tip: str) -> int:
    src = ROOT / "core" / "brand.py"
    if not src.exists():
        print("[X] 找不到 %s" % src)
        return 2

    text = _read(src)
    new, errs = inject_text(text, group, name, tip)
    if errs:
        for e in errs:
            print("[X] %s" % e)
        return 2

    # 注入后必须真的变了，否则打出来的包还是占位值 —— 提前拦下来，
    # 别等到包传上去了才发现用户群里写着 100000000。
    if group and group not in new:
        print("[X] 注入后文本里找不到群号 %s" % group)
        return 2
    if group and PLACEHOLDER_NO != group and ("\"" + PLACEHOLDER_NO + "\"") in new:
        print("[X] 注入后仍残留占位值 %s" % PLACEHOLDER_NO)
        return 2

    # 清空旧副本。只删我们自己建的目录，路径是写死的，不会误伤。
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    copied = 0
    for rel in COPY:
        srcp = ROOT / rel
        dstp = OUT_DIR / rel
        if not srcp.exists():
            print("[!] 跳过不存在的 %s" % rel)
            continue
        if srcp.is_dir():
            shutil.copytree(srcp, dstp,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          "*.pyo", ".pytest_cache"))
        else:
            dstp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcp, dstp)
        copied += 1

    _write(OUT_DIR / "core" / "brand.py", new)

    # persona.md 里也有一份群号 —— 它是注入到别人客户端里的人格正文，
    # 注入后跟程序彻底分离。这里不换掉的话，exe 界面上显示真群号、
    # 而写进客户端的 persona 里还是 100000000，用户从人格卡里抄到的是假号。
    #
    # app.py:persona_brand_ok() 每次启动会比对这两处，不一致就报出来 ——
    # 这个校验就是为「改了 A 忘了 B」准备的，这里正好把 B 一起改掉。
    if group:
        pp = OUT_DIR / "persona.md"
        if pp.exists():
            ptxt = _read(pp)
            old, err = _sub_text_placeholder(ptxt, group)
            if err:
                print("[X] persona.md 注入失败：%s" % err)
                return 2
            _write(pp, old)
            print("[OK] persona.md 群号已替换")

    print("[OK] 注入完成")
    print("     群号 %s" % group)
    if name:
        print("     群名 %s" % name)
    print("     副本 %s（%d 项）" % (OUT_DIR, copied))
    print("     下一步：cd \"%s\" && python -m PyInstaller --noconfirm "
          "--distpath dist --workpath _wp xiaoxiongmao.spec" % OUT_DIR)
    return 0


def _sub_text_placeholder(text: str, group: str) -> tuple[str, str]:
    """把 persona.md 里的占位群号换成真号。

    只替换**独立的**占位值（前后不是数字），避免误伤更长数字里的一段。
    真号本来就在 persona 里出现过的话直接返回 —— 重跑幂等。
    """
    if group in text:
        return text, ""
    pat = re.compile(r"(?<!\d)" + re.escape(PLACEHOLDER_NO) + r"(?!\d)")
    n = len(pat.findall(text))
    if n == 0:
        return text, "找不到占位值 %s" % PLACEHOLDER_NO
    return pat.sub(group, text), ""


def check(path: str) -> int:
    """检一个已注入的 brand.py：占位值还在不在、群号格式对不对。"""
    p = Path(path)
    if not p.exists():
        print("[X] 文件不存在：%s" % p)
        return 2
    text = _read(p)
    bad: list[str] = []

    m = re.search(r'^DEFAULT_GROUP_NO\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if not m:
        print("[X] 找不到 DEFAULT_GROUP_NO")
        return 2
    group = m.group(1)
    if not _RE_NO.match(group):
        bad.append("群号格式不对：%r" % group)
    if group == PLACEHOLDER_NO:
        bad.append("群号还是占位值 %s —— 这个包装出去用户加不进群" % PLACEHOLDER_NO)

    m2 = re.search(r'^DEFAULT_GROUP_NAME\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if not m2 or not m2.group(1).strip():
        bad.append("群名是空的")

    m3 = re.search(r'^DEFAULT_TIP\s*=\s*"([^"]*)"', text, re.MULTILINE)
    if not m3:
        bad.append("找不到 DEFAULT_TIP")

    if bad:
        for b in bad:
            print("[X] %s" % b)
        return 2

    print("[OK] %s" % p)
    print("     群号 %s" % group)
    print("     群名 %s" % m2.group(1))
    print("     话术 %s" % (m3.group(1)[:40] + ("…" if len(m3.group(1)) > 40 else "")))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="make_brand.py",
        description="把真实群号注入打包副本（源码仓里始终是占位值）")
    ap.add_argument("--group", metavar="号码",
                    help="真实群号，5-12 位数字。不给就只复制、不注入。")
    ap.add_argument("--name", metavar="群名", default="",
                    help="群名。不给就保留源码里的默认值。")
    ap.add_argument("--tip", metavar="话术", default="",
                    help="卡片底部那句话。不给就保留默认值。")
    ap.add_argument("--check", metavar="路径",
                    help="只检查一个已注入的 brand.py，不生成副本")
    a = ap.parse_args(argv)

    if a.check:
        return check(a.check)

    if a.group and not _RE_NO.match(a.group):
        print("[X] 群号应为 5-12 位数字：%r" % a.group)
        return 2

    return build(a.group or "", a.name, a.tip)


if __name__ == "__main__":
    sys.exit(main())