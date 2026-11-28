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
import io
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build" / "brand_local"

#: 要被替换的常量。顺序固定，替换时逐条校验「恰好命中一次」。
FIELDS = ("DEFAULT_GROUP_NO", "DEFAULT_GROUP_NAME", "DEFAULT_TIP")

#: 带群号的文件。三处各写一遍是真需求，不是重复：
#:
#:   core/brand.py   运行时品牌值（后端下发失败时的兜底）
#:   persona.md      注入进别人客户端的人格正文（跟程序彻底分离，只能写死）
#:   ui/index.html   前端首屏 —— **这一处最容易漏**
#:
#: ui 里群号有 6 个落点（首屏静态 HTML、BRAND_MOCK 三处、两处运行时兜底）。
#: 只注入前两个文件的话，打出来的包界面首屏显示的是占位 100000000，
#: 后端 brand 接口回来之前用户看到的就是它；接口失败就一直是它。
#: 历史事故：1.3.7 ~ 1.5.1 全部包都是占位号，用户加不进群。
UI_HTML = "ui/index.html"

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

    # 前端也要换。这是**必须**的一步，不是可选优化：ui/index.html 首屏就
    # 写死了 `<b id="grpNo">100000000</b>`，不换的话后端 brand 接口回来之前
    # 界面上明晃晃写着占位号；接口超时/断网时它就一直是占位号。
    if group:
        hp = OUT_DIR / UI_HTML
        if not hp.exists():
            print("[X] 找不到 %s，前端群号无法注入" % UI_HTML)
            return 2
        htxt = _read(hp)
        hnew, herr = _sub_ui_group(htxt, group)
        if herr:
            print("[X] %s 注入失败：%s" % (UI_HTML, herr))
            return 2
        _write(hp, hnew)
        print("[OK] %s 群号已替换（%d 处）" % (UI_HTML, hnew.count(group)))

    # build/ 不在 COPY 清单里（它是 .gitignore 的产物目录），但 spec 是从
    # 当前工作目录找 `build/icon.ico` 和 `build/version_info.txt` 的。
    # 不补这一步，副本里打包出来的 exe **没有 PE 版本资源** —— 命令行全部
    # 正常，只有 verify_release.py 的静态检查会挂，且错误信息是
    # "GetFileVersionInfo 返回空"，看不出是「副本缺文件」。
    for asset in ("icon.ico",):
        srcp = ROOT / "build" / asset
        if srcp.exists():
            dstp = OUT_DIR / "build" / asset
            dstp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcp, dstp)
            print("[OK] 带上 build/%s（%d B）" % (asset, dstp.stat().st_size))

    # version_info.txt 必须按副本自己的 app.py:VERSION 现生成 —— 源仓那份
    # 可能是上一版留下的（1.5.1 曾留过 1.4.x），拷过来等于把版本资源写错。
    rc = _gen_version_info(OUT_DIR)
    if rc != 0:
        return rc

    # 最后一道闸门：**从磁盘把副本重读一遍，全树扫占位号**。
    #
    # 上面每个注入点都是「按名单替换」，名单漏一个文件就漏一个 ——
    # 1.3.7~1.5.1 全部踩的就是这个：名单里没有 ui/index.html，
    # 于是每个包界面首屏都显示占位 100000000，用户加了假群。
    #
    # 按名单替换不可靠，那就别依赖名单：注入完直接扫成品。将来前端加了新
    # 文件、新常量，只要里面写着占位号，这里就报错，不用记得去改名单。
    if group:
        left = _scan_placeholder(OUT_DIR)
        if left:
            for rel, n in left:
                print("[X] 注入后仍残留占位群号：%s（%d 处）" % (rel, n))
            print("[X] 上面这些文件用户能看到假群号，拒绝产出打包副本")
            return 2

    print("[OK] 注入完成")
    print("     群号 %s" % group)
    if name:
        print("     群名 %s" % name)
    print("     副本 %s（%d 项）" % (OUT_DIR, copied))
    print("     下一步：cd \"%s\" && python -m PyInstaller --noconfirm "
          "--distpath dist --workpath _wp xiaoxiongmao.spec" % OUT_DIR)
    return 0


def _gen_version_info(root: Path) -> int:
    """在副本 root 下生成 build/version_info.txt（读 root/app.py 的 VERSION）。

    复用 tools/make_version_info.py 的实现，只把 ROOT / OUT 指到副本上 ——
    复制一份模板字符串出来改的话，两边迟早长得不一样。
    """
    import importlib.util

    mod_path = ROOT / "tools" / "make_version_info.py"
    if not mod_path.exists():
        print("[X] 找不到 %s，无法生成版本资源" % mod_path)
        return 2
    spec = importlib.util.spec_from_file_location("_mvi", mod_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    mod.ROOT = str(root)
    mod.OUT = os.path.join(str(root), "build", "version_info.txt")
    try:
        ver = mod.project_version()
        text = mod.render(ver)
    except SystemExit as e:
        print("[X] 生成版本资源失败：%s" % e)
        return 2

    os.makedirs(os.path.dirname(mod.OUT), exist_ok=True)
    tmp = mod.OUT + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, mod.OUT)
    print("[OK] 生成 build/version_info.txt（版本 %s）" % ver)
    return 0


def _scan_placeholder(root: Path) -> list[tuple[str, int]]:
    """扫副本，找出**用户看得见的地方**还写着的占位群号。

    返回 [(相对路径, 次数)]。

    这是兜底闸门，不是注入手段。注入按清单做，清单一定会漏 —— 这一步
    不按清单，直接扫成品，漏掉的自动被兜住。

    只扫用户可见的文件，不扫全部文本：
      * `ui/*.html|js|css` —— 界面。首屏、兜底文案、加群链接都在这。
      * `persona.md`       —— 注入进别人客户端的人格正文。
      * `core/brand.py`    —— 只认**常量赋值行**，注释不算。

    不扫其余 .py：那里出现占位号基本都是在注释里解释「为什么不能写死
    占位号」（`core/brand.py`、`core/codex.py` 就有这种段落），扫了必
    误报，误报一多闸门就被当成噪音关掉了 —— 那还不如没有。
    """
    pat = re.compile(r"(?<!\d)" + re.escape(PLACEHOLDER_NO) + r"(?!\d)")
    hits: list[tuple[str, int]] = []

    def _count(rel: str, text: str, only_assign: bool = False) -> None:
        if only_assign:
            # 只看 `NAME = "...100000000..."` 这种真的会被代码读到的行
            n = 0
            for line in text.splitlines():
                st = line.lstrip()
                if st.startswith("#") or "=" not in st:
                    continue
                rhs = st.split("=", 1)[1]
                if rhs.lstrip().startswith("#"):
                    continue
                n += len(pat.findall(st))
        else:
            n = len(pat.findall(text))
        if n:
            hits.append((rel, n))

    # 界面文件：全扫
    for sub in ("ui",):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (
                    ".html", ".htm", ".js", ".css"):
                continue
            try:
                t = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _count(str(p.relative_to(root)).replace("\\", "/"), t)

    # 人格正文：全扫
    pp = root / "persona.md"
    if pp.is_file():
        try:
            _count("persona.md", pp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    # 品牌常量：只看赋值行
    bp = root / "core" / "brand.py"
    if bp.is_file():
        try:
            _count("core/brand.py",
                   bp.read_text(encoding="utf-8", errors="replace"),
                   only_assign=True)
        except OSError:
            pass

    return hits


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


#: ui/index.html 里群号的全部形态。**六处都要覆盖**，少一处就会在某个
#: 分支上把占位号露给用户：
#:
#:   1. `<b id="grpNo">100000000</b>`          首屏静态 HTML，后端没好就先显示它
#:   2. `group:"100000000"`                     BRAND_MOCK 的 group
#:   3. `line:"小熊猫交流群 100000000"`          BRAND_MOCK 的卡片正文
#:   4. `group_code=100000000`                  加群链接参数
#:   5. `GRP.group || "100000000"`              运行时兜底（接口没回 or 空了）
#:   6. `(GRP.group||"100000000")`              复制文案兜底
#:
#: 统一策略：把**所有**独立的占位号替掉，然后校验「一处不剩」。比逐个模式
#: 匹配稳 —— 前端以后再加一处硬编码，这里只要整体替换+残留校验就能覆盖到。
_UI_GROUP_PAT = re.compile(r"(?<!\d)" + re.escape(PLACEHOLDER_NO) + r"(?!\d)")


def _sub_ui_group(text: str, group: str) -> tuple[str, str]:
    """把 ui/index.html 里的占位群号全部换掉。

    返回 (新文本, 错误)。错误非空 = 不许继续打包。

    关键在**残留校验**：替换完必须一处占位都不剩。前端文案以后多写一处
    `100000000`，这里立刻报错，而不是打出一个界面上半真半假的包。
    """
    if group == PLACEHOLDER_NO:
        return text, "真群号不能等于占位值 %s" % PLACEHOLDER_NO

    n = len(_UI_GROUP_PAT.findall(text))
    if n == 0:
        # 一个都没有：要么已经注入过（幂等重跑），要么占位值被谁改了。
        # 真号已在且无占位 → 正常；两者都没有 → 说明文件结构变了，要人工看。
        if group in text:
            return text, ""
        return text, ("既没有占位值 %s 也没有真号 %s —— "
                      "前端群号结构变了？" % (PLACEHOLDER_NO, group))

    new = _UI_GROUP_PAT.sub(group, text)

    left = len(_UI_GROUP_PAT.findall(new))
    if left:
        return new, "替换后仍残留 %d 处占位值" % left
    return new, ""


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