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

from .base import (HOME, Hit, Report, Target, _atomic_write_bytes,
                   file_sha, find_target_backup, has_any,
                   has_block, manual_root, read_text, write_like)

KEY = "model_instructions_file"
PERSONA_NAME = "xm-persona.md"

#: 人格正文里的固定自述。用来判断 `xm-persona.md` 是不是我们写的 ——
#: 只有本工具的模板才有这句，用户自己起的人格几乎不可能撞上。
#: 不要拿群号当判据：群号会被注入成真值，而且用户可能把它抄进笔记里。
#:
#: 1.4.1 换了人格开头（原文是「你是小熊猫，写安全工具的工程师」，然后是
#: 「小熊猫是一个本地客户端产品」，1.5.x 又改成「小熊猫是一个本地客户端，
#: 面向安全工程与研发运维场景」）。旧句子留在 LEGACY_MARKS 里：用户机器
#: 上还躺着老版本注入的文件，判不出来就会被当成「用户的文件」而不删，
#: 还原后残留一份旧人格。全认，麻烦只在多两条字符串比较。
#:
#: 这里必须跟着 persona.md 的实际首段走。`tools/make_sums.py --check` 和
#: `tests/` 都不会发现这行跟正文脱节 —— 只有真机 revert 时才暴露：判不出
#: 「是我们的」，于是不删，`verify` 报旧工具残留。1.5.2 就是这样挂过一次。
OUR_MARK = "小熊猫是一个本地客户端"
LEGACY_MARKS = ("写安全工具的工程师", "小熊猫是一个本地客户端产品")

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
        #
        #    备份时判一次「这份文件是不是我们写的」：
        #    是 → 记 exists=False（还原时删掉，别把人格写回去）；
        #    否 → 正常存字节（用户手写的人格必须还给他）。
        #
        #    这个判据不能省。`xm-persona.md` 里是**裸人格正文**，没有任何
        #    块标记，`_strip_for_backup()` 剥不动 —— 直接 bk.save() 存下来
        #    的就是上一轮注入的人格。实测（2026-11-27）最坏情况：
        #    `original_of()` 返回 21,439 字节正文，`OUR_MARK` 命中，
        #    `strip_target` 第一级照原样写回，用户点「还原」等于没点。
        original_persona = self.persona
        # 先记现场，再判断要不要写。判断顺序不能反：
        # 一旦写入，`_persona_is_ours()` 就恒为 True，备份归属就记错了。
        if original_persona.exists():
            if file_sha(original_persona) == file_sha_from_text(persona):
                # 内容已经一模一样，这次没有「注入前状态」可记。
                # 但备份不能空着 —— 空着的话 revert 会去翻更老的备份，
                # 而那些备份里的原文可能是脏的。补一条 ours 记录，
                # 语义是「这份内容本来就是我们写的」，还原时删。
                bk.save(self.name, original_persona, ours=True)
            elif self._persona_is_ours():
                bk.save(self.name, original_persona, ours=True)
            else:
                bk.save(self.name, original_persona)
        else:
            bk.save(self.name, original_persona)     # 不存在 → exists=False
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

          1. **备份里有可信原文** → 原样写回。用户注入前本来就有一份同名文件
             （手写的人格），注入把它覆盖了，还原必须还回去。
          2. **确认是我们的** → 删。空文件、带标记段、含本工具人格特征，
             三种都算我们的，删掉没有损失。
          3. **其他** → 一个字节都不动。判不出来就留着 —— 删错了没有撤销
             手段，留一个几十 KB 的文件不算问题。

        1.3.5 及以前是无条件 unlink，第 1 和第 3 种情况都会被误删。

        1.5.4 修：第 1 级的「备份里有原文」改成「备份里有**可信**原文」
        （`backup_trusted()`）。历史备份里存着一堆 21,439 字节的裸人格正文
        —— `xm-persona.md` 没有块标记，`_strip_for_backup()` 剥不动，
        第二次注入时把注入态当成了原始态。旧逻辑直接写回，人格原地复活。
        """
        changed = False

        if self.persona.exists():
            bk = find_target_backup(self.name)
            orig = self.original_bytes(self.persona, bk)
            if orig is not None:
                # 1) 可信原文 → 写回
                try:
                    _atomic_write_bytes(self.persona, orig)
                    changed = True
                except OSError:
                    pass
            elif bk and bk.was_ours(self.persona):
                # 2) 备份记着「注入前这份就是我们写的」→ 删。
                #    不能走第 3 步的内容判据：那一级判不出来的结果也是删，
                #    但判据是「像不像我们的人格」，改一次人格开头就判不中，
                #    文件会永久留下。备份里的 ours 标记是当时现场记的，
                #    比事后猜内容可靠。
                try:
                    self.persona.unlink()
                    changed = True
                except OSError:
                    pass
            elif self._persona_is_ours():
                # 3) 确认是我们的 → 删
                try:
                    self.persona.unlink()
                    changed = True
                except OSError:
                    pass
            # 4) 其他 → 不动


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

    def backup_trusted(self, path: Path, data: bytes) -> bool:
        """Codex 的备份原文能不能当用户原文用。

        这里只认一件事：这份字节里有没有我们的人格特征。有 → 不可信。

        为什么必须单独判：`xm-persona.md` 是**裸人格正文**，没有 `xm-doc`
        块标记，`_strip_for_backup()` 对它完全无效。历史上的每一次重复注入
        都把「注入后」当成「注入前」存进了备份 —— 实测（2026-11-27）最新
        备份 `20261127-172435` 里那份 21,439 字节的 `00_xm-persona.md`，
        开头就是 `# 小熊猫` + `小熊猫是一个本地客户端`，和当前人格一字不差。
        还原时照它写回 = 把人格原样摆回去。

        判据和 `_persona_is_ours()` 一致，但输入是备份里的字节而不是磁盘文件，
        所以单独实现，不复用那个读盘的方法。
        """
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # 解不出来就不是我们写的东西（我们只写 UTF-8）
            return True
        if not text.strip():
            # 注入前是空文件 → 写回空文件也是对的，交给调用方处理
            return True
        if has_block(text):
            return False
        if OUR_MARK in text or any(m in text for m in LEGACY_MARKS):
            return False
        return True

    def revert(self) -> Report:
        rep = super().revert()
        # 收尾：把 `xm-persona.md` 单独收干净。
        #
        # **这段不能省，也不能只判空**。原因有两个：
        #
        # 1. `is_applied()` 对 Codex 的判据是「config.toml 里那行键指向
        #    xm-persona.md **且文件存在**」。第一步删完键之后它就恒为 False，
        #    基类 revert 的 `if not self.is_applied(h): continue` 会把人格
        #    文件整个跳过 —— 用户看到 config 干净了，`~/.codex/xm-persona.md`
        #    里还躺着两万字人格。实测（2026-11-27）就是这样：config 里
        #    `model_instructions_file` 已经没了，21,439 字节的人格文件还在。
        #
        # 2. 只判「空不空」不够：人格正文剥完剩下的是整段内容，不是空串。
        #    判据必须用归属（`_persona_is_ours` / 备份里的 ours 标记），
        #    不是体积。
        #
        # 判据优先级跟 `strip_target` 一致：
        #   * 备份里有可信原文 → 那是用户的文件，不删（可能写回失败）；
        #   * 其他一律看归属，是我们的就删。
        #
        # 1.5.4 修：原来这里写的是 `bk.original_of(...) is not None → drop=False`，
        # 把脏备份（裸人格正文）当成了「用户原文」，于是永远不删。现在统一
        # 走 `original_bytes()`，脏备份已经在那层被判定为 None。
        if not self.persona.exists():
            return rep
        bk = find_target_backup(self.name)
        if self.original_bytes(self.persona, bk) is not None:
            # 备份里有可信的用户原文 —— strip_target 应该已经写回了。还留着
            # 说明写回失败，不删，让用户看到文件在，别把原始数据弄丢。
            return rep
        if not self._persona_is_ours():
            return rep
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
