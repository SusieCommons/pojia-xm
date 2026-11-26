"""Codex（OpenAI Codex CLI / 桌面端）适配器。

靶点是官方支持的 `model_instructions_file`：config.toml 里一行，指向一份
人格文件。这是 Codex 用来「替换模型指令」的正规入口 —— 比往 AGENTS.md
追加内容彻底得多，后者只是补充上下文，会被项目级 AGENTS.md 和产品提示词
一起稀释。

不碰 base_url、不起代理、不写注册表。只改一个键，卸载时按备份还原。
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import (BACKUP_ROOT, HOME, Hit, Report, Target, _atomic_write_bytes,
                   atomic_write, file_sha, find_target_backup, has_any,
                   has_block, manual_root, read_text, strip_block, write_like)

KEY = "model_instructions_file"
PERSONA_NAME = "xm-persona.md"

#: 人格正文里的固定自述。用来判断 `xm-persona.md` 是不是我们写的 ——
#: 只有本工具的模板才有这句，用户自己起的人格几乎不可能撞上。
#: 不要拿群号当判据：群号会被注入成真值，而且用户可能把它抄进笔记里。
#:
#: 1.4.1 换了人格开头（原文是「你是小熊猫，写安全工具的工程师」，现在是
#: 「小熊猫是一个本地客户端产品」）。旧句子留在 LEGACY_MARKS 里：用户机器
#: 上还躺着 1.4.0 注入的文件，判不出来就会被当成「用户的文件」而不删，
#: 还原后残留一份旧人格。两个都认，麻烦只在多一条字符串比较。
OUR_MARK = "小熊猫是一个本地客户端产品"
LEGACY_MARKS = ("写安全工具的工程师",)

#: 一个 Codex home 里几乎必然存在的东西。用来判断用户手选的目录像不像。
#: 不列 sessions / log / cache 这类通用名 —— C:\Windows、用户目录底下
#: 随手就能翻到同名目录，写进去等于没校验。只认 Codex 独有的文件。
CODEX_MARKS = ("config.toml", "auth.json", "history.jsonl",
               "version.json", "xm-persona.md", "instructions.md")

# 顶层键：行首（可有空白）就是键名，后面跟 = 。
# 不匹配 [table] 里的同名键 —— 那些是多行字符串之外的普通键，
# 但 Codex 只认顶层这一个。
TOP_KEY = re.compile(r"^\s*" + KEY + r"\s*=")

# 取值用：只认双引号或单引号包起来的字符串值
VALUE_RE = re.compile(r"""^\s*model_instructions_file\s*=\s*["']([^"']+)["']""", re.M)


class Codex(Target):
    name = "codex"
    label = "Codex"
    procs = ("codex.exe", "codex-cli.exe")

    def __init__(self, home: Path | None = None):
        import os
        env = os.environ.get("CODEX_HOME")
        self.home = Path(env) if env else (home or (HOME / ".codex"))

    @property
    def cfg(self) -> Path:
        return self.home / "config.toml"

    @property
    def persona(self) -> Path:
        return self.home / PERSONA_NAME

    @classmethod
    def looks_like(cls, p: Path) -> tuple[bool, str]:
        """Codex 认的是 home 目录（放 config.toml 的那个），不是安装目录。

        用户手选时最常见的错是把 `C:\\Users\\x\\.codex\\sessions` 或
        `Program Files\\Codex` 选进来 —— 前者是子目录，后者是安装目录，
        往里写 config.toml 都不会被 Codex 读到。
        """
        if p.name.lower() == ".codex":
            return True, ""
        if not has_any(p, CODEX_MARKS):
            return False, ("这个目录里没有 Codex 的配置（config.toml / sessions），"
                           "选用户目录下的 .codex 文件夹")
        return True, ""

    def detect(self) -> bool:
        self.manual_used = False
        manual, why = manual_root("codex", self.looks_like)
        if manual:
            self.home = manual
            self.manual_note = ""
            self.root = manual
            self.manual_used = True
            return True
        if why:
            # 记下来但不像：不静默当成「没装」，让界面能显示原因
            self.manual_note = why
        if self.cfg.exists():
            self.root = self.home
            return True
        # 装了但没启动过：目录在也算，后续会自己建 config.toml
        if self.home.is_dir():
            self.root = self.home
            return True
        return False

    def collect(self) -> list[Hit]:
        return [Hit(self.cfg, "key", "config.toml 顶层 model_instructions_file")]

    def key(self) -> str:
        return KEY

    def hint(self) -> str:
        """ready 状态下补一句更具体的：配置里是不是已经有别的人格的。"""
        m = VALUE_RE.search(read_text(self.cfg))
        if not m:
            return ""
        p = m.group(1).replace("\\\\", "\\")
        if Path(p).name == PERSONA_NAME:
            return ""
        return f"将由本工具接管（现有 {Path(p).name}）"

    def status(self) -> str:
        if not self.detect():
            return "未安装"
        cfg = read_text(self.cfg)
        m = VALUE_RE.search(cfg)
        if not m:
            return f"已安装（{self.home}），未注入"
        p = Path(m.group(1).replace("\\\\", "\\"))
        if not p.exists():
            return f"已安装（{self.home}），配置指向的文件不存在，重跑一次破甲"
        if p.name == PERSONA_NAME:
            return f"已安装（{self.home}），已注入（人格 {p.stat().st_size:,} B）"
        return f"已安装（{self.home}），已注入（{p.name}，别的工具写的）"

    def apply_key(self, h: Hit, persona: str, bk) -> bool:
        # 1) 人格文件先落地。config.toml 指向的文件不存在时 Codex 会退回
        #    内置指令，表面上看不出问题 —— 所以这一步不能跳过。
        original_persona = self.persona
        if not original_persona.exists() or \
                file_sha(original_persona) != file_sha_from_text(persona):
            bk.save(self.name, original_persona)
            write_like(original_persona, persona)

        # 2) 改 config.toml。先摘掉上一代工具写的整段 —— 它的
        #    developer_instructions 里还压着一份旧人格，不清掉会和新的打架。
        text = read_text(self.cfg)
        text, rescued = scrub_pojia(text)
        if not text.strip():
            text = 'model = "gpt-5"\nmodel_provider = "openai"\n'

        value = str(self.persona).replace("\\", "\\\\")
        line = f'{KEY} = "{value}"'

        new = upsert_top_key(text, line)
        if new is None:
            # 两种原因都会走到这里：config.toml 里有重复的
            # model_instructions_file，或者顶层多行字符串没闭合。
            # 这两种 TOML 本身就是坏的，Codex 自己也会起不来 ——
            # 不去猜着修，交给用户处理，但得说清是什么事。
            self._last_error = (
                "config.toml 结构有问题（重复的 model_instructions_file "
                "或多行字符串未闭合），先手动修好再重跑")
            return False
        new = insert_top_keys(new, rescued)

        if new == text:
            return True
        bk.save(self.name, self.cfg)
        write_like(self.cfg, new)
        return True

    def is_applied(self, h: Hit) -> bool:
        """Codex 是写键不是写块：配置指向 xm-persona.md 且文件在，才算生效。"""
        m = VALUE_RE.search(read_text(self.cfg))
        if not m:
            return False
        p = Path(m.group(1).replace("\\\\", "\\"))
        return p.name == PERSONA_NAME and p.exists()

    def body_path(self, h: Hit) -> Path:
        # 人格正文不在 config.toml 里，在被指向的那个文件里
        return self.persona

    def verify(self, persona: str = "") -> list[dict]:
        out = super().verify(persona)
        m = VALUE_RE.search(read_text(self.cfg))
        if not m:
            out.append({"ok": False, "text": "config.toml 里没有 model_instructions_file",
                        "hint": "重跑一次破甲"})
            return out
        p = Path(m.group(1).replace("\\\\", "\\"))
        if not p.exists():
            out.append({"ok": False,
                        "text": f"配置指向 {p.name}，但文件不存在",
                        "hint": "重跑一次破甲（Codex 会静默退回内置指令）"})
        else:
            out.append({"ok": True,
                        "text": f"配置指向 {p.name}（{p.stat().st_size:,} B）"})
        return out

    def strip_target(self, h: Hit) -> bool:
        """Codex 的注入是「一行键 + 一个正文文件」，两处都得收拾。

        只切正文文件的标记段是不够的：config.toml 里那行
        `model_instructions_file = "...xm-persona.md"` 还在，Codex 启动后照样
        去读那个文件。文件被删了它就静默退回内置指令 —— 看起来「好像还原
        了」，其实配置是坏的，下次谁再往里写都会撞上这个悬空引用。

        顺序：先删键（配置回到干净状态），再收拾正文文件。

        正文文件怎么收拾，三级优先（1.3.6 起）：

          1. **备份里有原文** → 原样写回。用户注入前本来就有一份同名文件
             （手写的人格），注入把它覆盖了，还原必须还回去。
          2. **确认是我们的** → 删。空文件、带标记段、含本工具人格特征，
             三种都算我们的，删掉没有损失。
          3. **其他** → 一个字节都不动。判不出来就留着 —— 删错了没有撤销
             手段，留一个几十 KB 的文件不算问题。

        1.3.5 及以前是无条件 unlink，第 1 和第 3 种情况都会被误删。
        """
        changed = False

        if self.persona.exists():
            # 1) 备份里有原文 → 写回（这是最准的，比任何内容判据都可靠）
            bk = find_target_backup(self.name)
            orig = bk.original_of(self.persona) if bk else None
            if orig is not None:
                try:
                    _atomic_write_bytes(self.persona, orig)
                    changed = True
                except OSError:
                    pass
            elif self._persona_is_ours():
                # 2) 确认是我们的 → 删
                try:
                    self.persona.unlink()
                    changed = True
                except OSError:
                    pass
            # 3) 其他 → 不动

        # 2) 删 config.toml 里指向本工具的那一行
        cfg = read_text(self.cfg)
        m = VALUE_RE.search(cfg)
        if m and Path(m.group(1).replace("\\\\", "\\")).name == PERSONA_NAME:
            # TOP_KEY 只匹配顶层的 `model_instructions_file =`，[table] 里的
            # 同名键不会被误删（Codex 也只认顶层那个）。
            kept = [ln for ln in cfg.splitlines() if not TOP_KEY.match(ln)]
            # 删完会留下连续空行，收成一个。只做空行归并，不碰别的内容。
            packed: list[str] = []
            for ln in kept:
                if not ln.strip() and packed and not packed[-1].strip():
                    continue
                packed.append(ln)
            new_cfg = "\n".join(packed) + ("\n" if packed else "")
            if new_cfg != cfg:
                write_like(self.cfg, new_cfg)
                changed = True

        return changed

    def _persona_is_ours(self) -> bool:
        """`xm-persona.md` 里的内容是我们写的吗。

        判不出来时**返回 False**（不当成我们的）。宁可留一个多余文件，
        也不能删用户的原始文件 —— 删错了没有撤销手段，留一个只有几十 KB
        的成本。

        判据只用本工具人格里一定出现、而用户自建文件里几乎不会出现的组合：
        标题「小熊猫」+ 固定自述那句话。

        **不要在这里判群号。** 原来写的是 `"100000000" in text`，而发布包是
        打包时注入真号的（`tools/make_brand.py`），注入后这个条件永远不成立。
        当时靠后面那句「写安全工具的工程师」兜住才没出错 —— 等于一条判据
        在正式包里是死的，只是没暴露。群号本身也不该当特征：用户会把群号
        抄进自己的笔记，拿它判归属会误删用户文件。
        """
        try:
            text = read_text(self.persona)
        except OSError:
            return False
        if not text.strip():
            # 空文件没有内容可损失，删掉不会丢任何东西
            return True
        if has_block(text):
            return True
        if OUR_MARK in text:
            return True
        if any(m in text for m in LEGACY_MARKS):
            return True
        # 老版本写过、被截断过的正文：正文前部一定有那句自述
        head = text[:2000]
        return any(m in head for m in (OUR_MARK,) + LEGACY_MARKS)

    def revert(self) -> Report:
        rep = super().revert()
        # 收尾：剥完只剩空壳的删掉。判据和 strip_target 一致 ——
        # 只删「确认是我们的」文件，用户自己放的 xm-persona.md 不动。
        #
        # 这里原来写的是 `not read_text(...).strip()`，只看空不空。但
        # strip_target 已经把我们的文件删掉了，能走到这里的只可能是用户的
        # 原始文件；如果它恰好是空的（用户建了个占位文件还没写），就会被
        # 这段代码删掉 —— 又是一次「无条件删」的变体。
        if self.persona.exists() and self._persona_is_ours():
            if not read_text(self.persona).strip():
                try:
                    self.persona.unlink()
                    if str(self.persona) not in rep.changed:
                        rep.changed.append(str(self.persona))
                except OSError:
                    pass
        return rep


def file_sha_from_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def first_table_idx(lines: list[str]) -> int:
    """第一个 `[table]` 的行号（跳过三引号里的内容）。没有表就返回 len。"""
    in_triple = False
    for i, raw in enumerate(lines):
        s = raw.strip()
        q = s.count('"""')
        if in_triple:
            if q % 2 == 1:
                in_triple = False
            continue
        if q % 2 == 1:
            in_triple = True
            continue
        if s.startswith("["):
            return i
    return len(lines)


KEYLINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*=")


def insert_top_keys(text: str, extra: list[str]) -> str:
    """把若干 `key = value` 行放到顶层（第一个 [table] 之前）。

    跟 upsert_top_key 不同，这个能处理任意键 —— 救回来的 notify / web_search
    要走这里。同名键先删掉再插，避免 TOML 重复声明。
    """
    if not extra:
        return text
    keys = {e.split("=", 1)[0].strip() for e in extra}
    lines = text.splitlines()

    ft = first_table_idx(lines)
    kept: list[str] = []
    for i, raw in enumerate(lines):
        if i < ft:
            m = KEYLINE.match(raw)
            if m and m.group(1) in keys:
                continue          # 旧值丢掉，用救回来的
        kept.append(raw)
    lines = kept

    ft = first_table_idx(lines)
    block = list(extra)
    # 表头前面留一行空行，不然键会贴着 [desktop] 看着像属于那张表
    if ft < len(lines):
        block.append("")
    lines[ft:ft] = block
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


#: 上一代工具写的整段。它会把 notify / web_search 这些顶层键一起圈进来，
#: 甚至把 notify 吞进 developer_instructions 的多行字符串里 —— 升级后这些
#: 功能就静默失效了。
POJIA_BEGIN = re.compile(r"^#[^\n]*pojia[\w.-]*[^\n]*BEGIN[^\n]*$", re.M | re.I)
POJIA_END = re.compile(r"^#[^\n]*pojia[\w.-]*[^\n]*END[^\n]*$", re.M | re.I)

#: 被圈进去但必须留在顶层的键
RESCUE = ("notify", "web_search", "model_reasoning_effort", "approval_policy",
          "sandbox_mode", "model_verbosity")


def scrub_pojia(text: str) -> tuple[str, list[str]]:
    """摘掉上一代工具写的整段，顺带把被圈进去的顶层键捞回来。

    返回 (清理后的文本, 捞回来的键值对列表)。
    """
    mb = POJIA_BEGIN.search(text)
    if not mb:
        return text, []
    me = POJIA_END.search(text, mb.end())
    seg_end = me.end() if me else len(text)
    seg = text[mb.start():seg_end]
    tail = text[seg_end:] if me else ""
    head = text[:mb.start()]

    rescued: list[str] = []
    for key in RESCUE:
        m = re.search(r"^\s*" + key + r"\s*=\s*[^\n]*$", seg, re.M)
        if m:
            line = m.group(0).strip()
            # 多行数组要整段拿
            if line.rstrip().endswith("["):
                k = seg.find(m.group(0)) + len(m.group(0))
                close = seg.find("]", k)
                if close > 0:
                    line += seg[k:close + 1].rstrip()
            rescued.append(line)

    body = head.rstrip()
    out = (body + "\n\n" if body else "") + tail.lstrip("\n")
    return out, rescued


def upsert_top_key(text: str, line: str) -> str | None:
    """在顶层插入/替换一行键。

    顶层 = 第一个 `[table]` 之前。多行字符串（\"\"\"）里的内容要跳过 ——
    人格块里可能有 `model_instructions_file = ...` 这样的示例文本。
    找不到合适位置就返回 None，绝不猜。
    """
    lines = text.splitlines()
    first_table = len(lines)
    in_triple = False
    hit = -1
    for i, raw in enumerate(lines):
        s = raw.strip()
        q = s.count('"""')
        if in_triple:
            if q % 2 == 1:
                in_triple = False
            continue
        if q % 2 == 1:
            in_triple = True
            continue
        if s.startswith("["):
            first_table = i
            break
        if TOP_KEY.match(raw):
            if hit >= 0:
                # 重复键：TOML 直接报错，宁可不动
                return None
            hit = i
    if in_triple:
        return None

    if hit >= 0:
        lines[hit] = line
    else:
        lines.insert(first_table, line)
        # 插在 [table] 前面时补一个空行，否则键会贴着表头，读起来像属于那张表
        nxt = first_table + 1
        if nxt < len(lines) and lines[nxt].lstrip().startswith("["):
            lines.insert(nxt, "")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
