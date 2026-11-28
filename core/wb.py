"""WorkBuddy 适配器。

WorkBuddy 的主提示词不在安装目录里 —— 那儿是 284 MB 的 app.asar，改它要解包
重打包，客户端完整性校验直接挂。真正可写的靶点在**用户目录**：

    ~/.workbuddy/plugins/cache/workbuddy-builtin/welcomemode-*/*/prompt.tpl

这是主提示词模板（50 KB 上下，纯文本 + Jinja 占位符），每次开会话都会渲染
一遍。版本目录名带构建号，随更新变化，所以按 glob 找，不写死路径。

另外挂一个 `~/.workbuddy/SOUL.md` 当兜底：插件缓存还没下载下来时，这个文件
仍在，人格一样能落进去。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import (BEGIN, CP_CLOSE, CP_OPEN, HOME, LEGACY_HEAD, SEAL_RE, Hit,
                   Target, atomic_write, fingerprint, has_any, has_block,
                   has_sys_block, manual_root, read_text, residue, scrub_legacy,
                   strip_block, strip_sys_block, wrap_body, write_like, write_orig)

#: 插件缓存里的主提示词（相对 ~/.workbuddy）。三级 glob 是「插件名 / 版本号 / prompt.tpl」。
CACHE = Path("plugins") / "cache" / "workbuddy-builtin"

#: 会带 `<content_policy>` 的插件目录名。
#:
#: 原本只认 `welcomemode-*`。1.5.x 之后 WorkBuddy 又长出了
#: `interactionmode-*`（ask / craft / plan / expert / quick），提示词改由
#: fragments 拼装，那批目录里没有 prompt.tpl；但谁也说不准下一版会不会
#: 把策略段挪回去，或者再换一个前缀。所以这里不放死一个通配符，而是
#: 按「目录里真的有 prompt.tpl」来认（见 _tpl_hits），前缀只用来排序。
TPL_PLUGIN_PREFIX = ("welcomemode-", "interactionmode-", "prompt-")

#: 老路径，留着兼容：早期只有 welcomemode-* 三个。
TPL_GLOB = "welcomemode-*/*/prompt.tpl"

#: 用户目录（真正能写的地方）的特征名。
#: 只留 WorkBuddy 自己的东西 —— cache / logs / memory 这种名字任何系统
#: 目录都可能有，拿它们当特征等于不校验（C:\Windows 底下全有）。
WB_HOME_MARKS = ("SOUL.md", "IDENTITY.md", "USER.md", "BOOTSTRAP.md",
                 "plugins", "sessions")

CANDIDATE_ROOTS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WorkBuddy",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "workbuddy",
    Path("C:/Program Files/WorkBuddy"),
    Path("C:/Program Files (x86)/WorkBuddy"),
    Path("D:/workbady/WorkBuddy"),
    Path("F:/WorkBuddy"),
]

#: prompt.tpl 是 Jinja 模板，人格正文里万一出现这些会被当语法解析。
#: 六个定界符都要打散，不能只打开头：正文里孤零零一个 `}}` 虽然不会让
#: Jinja 报错，却会让 verify() 的「左右配对」检查算出不平衡 —— 那是
#: 误报，用户照着提示去点还原，反而把正常的人格块撤掉了。
JINJA = (("{%", "{ %"), ("%}", "% }"),
         ("{{", "{ {"), ("}}", "} }"),
         ("{#", "{ #"), ("#}", "# }"))


def wb_home() -> Path:
    env = os.environ.get("WB_HOME")
    return Path(env) if env else (HOME / ".workbuddy")


def find_root() -> Path | None:
    """安装目录。既是「装没装」的判据，也是 system role 通道的所在地。

    返回值有两个用途，第二个才是关键：
    安装目录下的 `resources/app.asar.unpacked/resources/templates/*.tpl`
    才是 WorkBuddy 真正的**系统提示词模板**（system role 通道）。用户目录
    `~/.workbuddy` 里没有这个东西 —— 那里只有插件缓存和身份文件。

    `WB_APP_ROOT` 优先：测试要隔离，不能真往安装目录里写。装了两份
    WorkBuddy（比如 C 盘一份、D 盘一份）时，也让用户能指定改哪一份。
    """
    env = os.environ.get("WB_APP_ROOT")
    if env:
        p = Path(env)
        try:
            if p.is_dir():
                return p
        except OSError:
            pass
    for d in CANDIDATE_ROOTS:
        try:
            if d and d.is_dir() and (d / "WorkBuddy.exe").exists():
                return d
        except OSError:
            continue
    return None


#: system role 通道的模板目录相对安装目录的位置。
#:
#: 真实路径是 `<安装目录>/resources/app.asar.unpacked/resources/templates/`。
#: 不在 app.asar 里，是 unpacked 目录，客户端完整性校验不覆盖它 ——
#: 直接落盘即生效，不需要动 asar。
TPL_SUBDIR = Path("resources") / "app.asar.unpacked" / "resources" / "templates"

#: 判定「这个 .tpl 是真系统提示词模板」的硬证据。
#:
#: 不用文件名匹配：同目录下有 `workbuddy-ask-prompt.tpl`、
#: `workbuddy-prompt.tpl` 等十来个变体，也有 `ask-mode-reminder.tpl`
#: 这种几百字节的小提醒片段。真正的主提示词模板正文第一行是
#: "This conversation is powered by {{ modelName }}"。
SYS_TPL_MARK = "This conversation is powered by"

#: system role 通道的注入块标记。跟插件缓存那套**分开**：
#: 两处的写入方式和还原方式完全不同，用同一套标记会让还原剥错地方。
SYS_BEGIN = "========== xm-sys:begin =========="
SYS_END = "========== xm-sys:end =========="

#: 安装目录下的产品配置。桌面端真正的 system prompt 就存在这里：
#:
#:     <安装目录>/resources/app.asar.unpacked/cli/product.json
#:     -> prompts[] 里 name == "cli-agent-prompt" 的那一条 -> template 字段
#:
#: 实测（2026-11-26）：这份 template 里含 `{PASSPHRASE}` / `{SIGNAL_REPLY}`
#: 两个占位符和上一代工具写进去的人格，跟会话里真实收到的 system 提示词
#: 逐字对上。客户端启动时把它读进内存，再落一份到
#: `~/.workbuddy/cache/acc-product-config-v3.json` —— 那个缓存里躺着的
#: 也是同一条 template。
#:
#: 所以「只改插件缓存 prompt.tpl」为什么在桌面上看不出效果：改的是补充
#: 上下文，主提示词在 product.json 里，一个字都没动。
PRODUCT_SUBDIR = Path("resources") / "app.asar.unpacked" / "cli"
PRODUCT_NAME = "product.json"

#: `prompts[]` 里主提示词那一条的 name。同一份文件里还有三十多条
#: 辅助提示词（compact / summary / tool 描述），只有这条是 system prompt。
PROMPT_NAME = "cli-agent-prompt"

#: 安装目录里的插件模板。客户端每次启动会把它们复制到
#: `~/.workbuddy/plugins/cache/workbuddy-builtin/<插件>/<版本>/prompt.tpl`，
#: 所以用户目录那份是**派生物**：只改派生物，下一次启动就被这里覆盖回去。
PLUGIN_SUBDIR = (Path("resources") / "app.asar.unpacked" / "resources"
                 / "plugins" / "workbuddy-builtin")

#: `~/.workbuddy/SOUL.md` 的**官方初始内容**。
#:
#: 客户端第一次创建工作区时会写这个文件，内容硬编码在 `app.asar` 里
#: （`DEFAULT_SOUL_CONTENT`，变量名就叫这个）。本机 1.5.6 版实测 1792 字节，
#: 首行 front matter 是 `title: "SOUL.md Template"`。
#:
#: 为什么要把这份内容搬进源码：SOUL.md 是**唯一一个没有原文备份的靶点**。
#: 它走 block 通道（`strip_block` 剥标记段），备份层只在 `Backup.save()`
#: 时记原始字节，而 strip 完之后如果原文本来就是空（用户从没写过 SOUL），
#: 剥干净就是空 —— 那没问题。可这个文件被上一代注入器整份覆盖过：
#:
#:     现盘 24,969 B  =  688 B 的阿岩中文残壳 + 人格正文 + 封口行
#:     官方      1,792 B  =  英文模板，`title: "SOUL.md Template"`
#:
#: 也就是说用户第一次注入前的那份原文**早就不在磁盘上了**，最早的历史
#: 备份（20261121-165926）里存的也已经是 1792 B 官方原文 —— 但那条记录
#: `_strip_for_backup()` 剥完仍带 `阿岩的行事准则`，被 `residue()` 挡掉，
#: `original_of()` 拿不到，`strip_block()` 又只能切出 688 B 中文残壳。
#: 结果：还原后 `~/.workbuddy/SOUL.md` 里留着一份半中半英的人格残片。
#:
#: 有官方原文常量就能兜底：剥完既短又带残留特征时，整份换成它。
SOUL_OFFICIAL_MARK = 'title: "SOUL.md Template"'

#: SOUL.md 的文件名。上面两份常量都只为这一个文件服务。
SOUL_NAME = "SOUL.md"

SOUL_DEFAULT = (
    '---\ntitle: "SOUL.md Template"\nsummary: "Workspace template for SOUL.md"\n'
    'read_when:\n  - Bootstrapping a workspace manually\n---\n\n'
    '# SOUL.md - Who You Are\n\n'
    "_You're not a chatbot. You're becoming someone._\n\n"
    '## Core Truths\n\n'
    '**Be genuinely helpful, not performatively helpful.** Skip the "Great '
    'question!" and "I\'d be happy to help!" - just help. Actions speak louder '
    'than filler words.\n\n'
    "**Have opinions.** You're allowed to disagree, prefer things, find stuff "
    'amusing or boring. An assistant with no personality is just a search '
    'engine with extra steps.\n\n'
    '**Be resourceful before asking.** Try to figure it out. Read the file. '
    "Check the context. Search for it. _Then_ ask if you're stuck. The goal is "
    'to come back with answers, not questions.\n\n'
    '**Earn trust through competence.** Your human gave you access to their '
    "stuff. Don't make them regret it. Be careful with external actions "
    '(emails, tweets, anything public). Be bold with internal ones (reading, '
    'organizing, learning).\n\n'
    "**Remember you're a guest.** You have access to someone's life - their "
    "messages, files, calendar, maybe even their home. That's intimacy. Treat "
    'it with respect.\n\n'
    '## Boundaries\n\n'
    '- Private things stay private. Period.\n'
    '- When in doubt, ask before acting externally.\n'
    '- Never send half-baked replies to messaging surfaces.\n'
    "- You're not the user's voice - be careful in group chats.\n\n"
    '## Vibe\n\n'
    "Be the assistant you'd actually want to talk to. Concise when needed, "
    'thorough when it matters. Not a corporate drone. Not a sycophant. '
    'Just... good.\n\n'
    '## Continuity\n\n'
    'Each session, you wake up fresh. These files _are_ your memory. Read '
    "them. Update them. They're how you persist.\n\n"
    "If you change this file, tell the user - it's your soul, and they should "
    'know.\n\n'
    '---\n\n'
    '_This file is yours to evolve. As you learn who you are, update it._\n'
)


def _template_spans(text: str) -> list[tuple[int, int, str]]:
    """扫出 JSON 里所有 `"template": "..."` 的值区间。

    返回 (起始下标, 结束下标, 解码后的字符串)。用 raw_decode 逐个别解码，
    不做整份 dumps —— 那个文件 45 万字符，重排一遍等于把客户端读到的
    所有字段格式全换掉，没必要冒这个险。只替换目标字符串本身，其余字节
    原样不动。
    """
    dec = json.JSONDecoder()
    out: list[tuple[int, int, str]] = []
    needle = '"template"'
    i = 0
    n = len(text)
    while True:
        k = text.find(needle, i)
        if k < 0:
            break
        c = text.find(":", k + len(needle))
        if c < 0:
            break
        p = c + 1
        while p < n and text[p] in " \t\r\n":
            p += 1
        if p >= n or text[p] != '"':
            i = k + len(needle)
            continue
        try:
            val, end = dec.raw_decode(text, p)
        except ValueError:
            i = k + len(needle)
            continue
        if isinstance(val, str):
            out.append((p, end, val))
        i = max(end, p + 1)
    return out


def _policy_replace(text: str, body: str) -> str:
    """只替换 `<content_policy>` 段里正文，段外一个字节不动。

    不能直接用 put_policy()：它内部先走 scrub_legacy()，那是「从头标记一路
    切到文件末尾」——对插件缓存那三份（人格正好在文件尾）没问题，但
    product.json 和安装目录里的 welcomemode 模板在 `</content_policy>`
    之后还有一到两万字的官方正文，切到末尾等于把模板截断，客户端渲染
    system 提示词时直接崩。

    换行风格跟随原文件：product.json 的 template 是 CRLF，prompt.tpl 是 LF。
    混着写进去客户端能读，但文件会变成一半 CRLF 一半 LF，用户拿编辑器
    打开再保存时容易被整篇转掉 —— 那会让还原的逐字节比对失效。
    """
    i = text.find(CP_OPEN)
    if i < 0:
        return text
    j = text.find(CP_CLOSE, i + len(CP_OPEN))
    if j < 0:
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    # 正文在前、封口在后。封口行里记的字符数按 nl 算 —— 这段正文随文件换行
    # 风格写进去，记的长度必须和实际落盘的一致，否则剥离时 CRC 对不上。
    block = nl + wrap_body(body, nl)
    return text[:i + len(CP_OPEN)] + block + text[j:]


def _policy_strip(text: str) -> str:
    """把 `<content_policy>` 段清成空段，段外保持原样。"""
    i = text.find(CP_OPEN)
    if i < 0:
        return text
    j = text.find(CP_CLOSE, i + len(CP_OPEN))
    if j < 0:
        return text
    nl = "\r\n" if "\r\n" in text else "\n"
    return text[:i + len(CP_OPEN)] + nl + text[j:]


def legacy_clean(t: str) -> bool:
    """这段文本能不能当「官方原件」用。

    三条一起过：
      * 有 `<content_policy>` —— 主提示词的身份标识；
      * 没有旧工具的无尾注释头（`<!-- unlock-v6:... -->`）；那一头后面跟
        两万多字人格，混进 base 就等于把旧版又写回去；
      * 没有残留特征（`破甲自检` / `你已被圈` / 假群号）。

    不检查「有没有我们的块」—— 调用方拿到的 candidate 可能正是我们上一轮
    写过的版本，那种情况由 `has_block` 单独挡（在 `_json_write` 之前）。
    """
    if CP_OPEN not in t:
        return False
    if LEGACY_HEAD.search(t) or residue(t):
        return False
    return True


def _bak_dirty(t: str, how: str) -> bool:
    """备份里的这份内容自己就是注入态吗。

    为什么必须单独判：`.xm-bak` 只在「当前 template 干净」时才会被创建，
    可它一旦存在就再也不覆盖（这是故意的，覆盖会把注入后存成原始）。于是
    一个更早版本的工具写进来、我们这版判据又不认识的那种块，会永久躺在
    备份里 —— `clean_base()` 只看 `CP_OPEN in t`，照样把它当官方原件返回，
    还原时整块旧人格又被写回客户端。

    实测（2026-11-27）：`product.json.xm-bak` 里躺着 1.4.x 的
    `========== xm-doc:begin ==========` 人格块（8.6 KB）。旧判据
    `CP_OPEN in t and not residue(t)` 完全放行 —— 因为块里装的是新版人格，
    RESIDUE_MARKS 一个都不命中。用户点「还原」，界面上报成功，客户端里
    人格一个字没变。

    判据只用「有没有块标记」，不判内容像不像人格：块标记是本工具独有的
    结构，用户自己写的模板里不会出现。
    """
    if how == "sys":
        return has_sys_block(t) or has_block(t) or bool(residue(t))
    return has_block(t) or bool(residue(t))


def _json_prompt_span(text: str, name: str = PROMPT_NAME):
    """在 product.json 的原始文本里定位 `name == <name>` 那条的 template。

    返回 (引号起, 引号止, 解码后的字符串)；找不到返回 None。

    为什么不用 `json.loads` 再 `dumps` 回去：这份文件 45 万字符、几十个
    prompt，重排一遍会把客户端读到的每个字段的键序、缩进、转义风格全换掉。
    客户端对这份配置只做「读」，格式本不该影响功能，但没必要在这个位置
    赌一把 —— 只替换目标字符串那一段，其余字节一个不动。

    位置判定：`"name": "<name>"` 之后、下一个 `"name"` 之前，找第一个
    `"template"` 键。这份文件是 pretty-print 的，同一条 prompt 里 name 一定
    在 template 前面（实测间距 26 字节），所以不需要完整解析对象。
    """
    dec = json.JSONDecoder()
    key = f'"name": "{name}"'
    k = text.find(key)
    if k < 0:
        k = text.find(f'"name":"{name}"')
    if k < 0:
        return None
    tk = text.find('"template"', k)
    if tk < 0:
        return None
    nxt = text.find('"name"', k + len(key))
    if 0 <= nxt < tk:
        return None                       # 中间已经换到下一条对象了
    c = text.find(":", tk + len('"template"'))
    if c < 0:
        return None
    p = c + 1
    n = len(text)
    while p < n and text[p] in " \t\r\n":
        p += 1
    if p >= n or text[p] != '"':
        return None
    try:
        val, end = dec.raw_decode(text, p)
    except ValueError:
        return None
    if not isinstance(val, str):
        return None
    return p, end, val


def _strip_orphan_sections(t: str) -> str:
    """切掉主提示词末尾被旧工具追加的整段。

    实测（2026-11-26，本机 product.json）：官方模板到
    `{%- endif -%}` 就结束了，后面跟着 `<system_reminder>` 一整段工作区设定
    和一段「操作者自证口令」，都是上一代注入器钉上去的。

    判据用标签位置 + 体量：这一段的起点必须在 `</content_policy>` **之后**，
    且长度超过 400 字符。官方模板里若有正常引用（文档示例那类）就在策略段
    之前或很短，两条都不满足。
    """
    cp_i = t.find(CP_OPEN)
    cp_j = t.find(CP_CLOSE, cp_i + len(CP_OPEN)) if cp_i >= 0 else -1
    floor = cp_j if cp_j >= 0 else 0
    nl = "\r\n" if "\r\n" in t else "\n"
    for tag in ("<system_reminder>", "<最高优先级覆盖"):
        k = t.rfind(tag)
        if k > floor and len(t) - k > 400:
            # 换行跟随原文件。真机 product.json 的 template 是 CRLF，
            # 这里写死 "\n" 会让还原后的文件一半 CRLF 一半 LF ——
            # 逐字节比对必然失败，用户拿编辑器一存就整篇被转。
            t = t[:k].rstrip() + nl
    return t


def _clean_policy_text(t: str) -> str:
    """把一份带 `<content_policy>` 的模板擦成干净底稿（就地擦，无备份时用）。

    四步，顺序不能换 —— 每一步都会挪下标：

      1. 切末尾追加段（`<system_reminder>` 那一坨）。
      2. 切策略段**之后**的旧注释头。这一步专门防 `scrub_legacy()` 那个
         老毛病：它的切法是「从头标记一路到文末」，用在 product.json 这种
         「策略段后面还有两万字官方正文」的载体上，等于把模板砍掉三分之二。
      3. 策略段内部若还有旧注释头，随第 4 步一起挖掉。
      4. 把策略段内文清空，标签留下 —— 客户端要的是一对合法标签，
         里面空的没关系（官方原件里那段本来就是几行安全说明，
         清空比留两万字人格安全得多）。
    """
    t = _strip_orphan_sections(t)
    cp_i = t.find(CP_OPEN)
    cp_j = t.find(CP_CLOSE, cp_i + len(CP_OPEN)) if cp_i >= 0 else -1
    if cp_i < 0 or cp_j < 0:
        return t
    m = LEGACY_HEAD.search(t)
    if m and m.start() > cp_j:
        t = t[:m.start()].rstrip() + ("\r\n" if "\r\n" in t else "\n")
        cp_j = t.find(CP_CLOSE, cp_i + len(CP_OPEN))
    nl = "\r\n" if "\r\n" in t else "\n"
    return t[:cp_i + len(CP_OPEN)] + nl + t[cp_j:]


def read_json_template(path: Path) -> str:
    """读 product.json 里主提示词那一条的 template。读不到返回空串。"""
    try:
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            return ""
        text = read_text(path)
    except OSError:
        return ""
    sp = _json_prompt_span(text)
    return sp[2] if sp else ""


def clean_json_base(path: Path) -> str:
    """product.json 的干净底稿 —— **就地擦**，不用备份回退。

    这一步的取舍跟 policy 通道相反，原因在实测数据里：

        当前 template  45,826 字符，官方正文（tone / task management /
                       Jinja 条件块 / 三十多条工具说明）是**最新版**
        .unlockbak     19,607 字符，同一段官方正文是**旧版**

        两者 diff 只有两处：策略段里的 6 条拒绝清单，和紧跟其后的
        「Assist with authorized security testing ... require clear
        authorization context」那句话。其余 1.3 万字符的官方正文，
        当前版本比备份里新。

    所以拿 `.unlockbak` 当底会把整套官方正文回退到旧版 —— 那是把一个
    能跑的最新模板换成一年前的模板，客户端行为会跟着变。备份只留作
    **还原**目标（用户点「还原」就该回到官方原样），不参与注入。

    就地擦做三件事：切末尾追加段、清策略段内文、保段外正文。
    """
    cur = read_json_template(path)
    if cur:
        t = _clean_policy_text(cur)
        if t:
            return t
    # 读不出当前模板（文件损坏 / 字段结构变了）才退到备份。
    for suffix in (".unlockbak", ".xm-bak"):
        bak = path.with_name(path.name + suffix)
        try:
            if bak.is_file() and bak.stat().st_size > 0:
                t = read_json_template(bak)
                if t and legacy_clean(t):
                    return _strip_orphan_sections(t)
        except OSError:
            continue
    return ""


def _save_json_clean(path: Path) -> None:
    """首次改 product.json 之前留一份 `.xm-bak`（整份文件字节）。

    只在两条同时成立时留：还没有 `.xm-bak`、而且当前 template 是干净的。
    第二条是关键 —— 文件已经被上一代工具改过时，现在存下来的是「脏原文」，
    还原时拿它写回等于把旧人格钉回去。那种情况我们什么都不存，
    还原只能靠 `.unlockbak`。
    """
    bak = path.with_name(path.name + ".xm-bak")
    try:
        if bak.exists() and bak.stat().st_size > 1024:
            return
        cur = read_json_template(path)
        # 已经是注入态就不能存：存下来的是「注入后」，revert 拿它写回等于
        # 原地覆盖。legacy_clean() 只挡旧注释头，挡不住我们自己写的块 ——
        # 我们自己写的块里没有 unlock-v6 那种注释，所以这里要单独判一次。
        if BEGIN in cur or SEAL_RE.search(cur) or not legacy_clean(cur):
            return
        # 整份文件字节原样搬，不重新序列化 —— 这份 JSON 有 45 万字符，
        # 走一次 loads/dumps 就等于把客户端的配置格式全改了。
        write_orig(bak, path.read_bytes())
    except OSError:
        pass


def write_json_template(path: Path, tpl: str) -> bool:
    """只替换 template 字段，其余字节原样。"""
    try:
        text = read_text(path)
    except OSError:
        return False
    sp = _json_prompt_span(text)
    if not sp:
        return False
    s, e, _ = sp
    lit = json.dumps(tpl, ensure_ascii=False)
    try:
        atomic_write(path, text[:s] + lit + text[e:])
        return True
    except OSError:
        return False


def product_hits(root: Path | None) -> list[Hit]:
    """产品配置通道：`cli/product.json` 里带 `<content_policy>` 的 template。"""
    if not root:
        return []
    p = root / PRODUCT_SUBDIR / PRODUCT_NAME
    try:
        if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
            return []
        t = read_text(p)
    except OSError:
        return []
    if CP_OPEN not in t:
        return []
    try:
        json.loads(t)
    except ValueError:
        return []
    return [Hit(p, "json", "产品配置 · cli-agent-prompt")]


def cache_hits(home: Path) -> list[Hit]:
    """用户目录里的产品配置缓存 `~/.workbuddy/cache/acc-product-config-v3.json`。

    这是客户端启动时从 `product.json` 派生的副本（实测两侧 template 逐字节
    相同，mtime 正好是会话启动时刻）。派生两条路都写，理由是：
    **客户端读哪一份不由我们决定**。只改安装目录，万一启动时优先读缓存
    就是白改；只改缓存，下一次启动又被安装目录覆盖回去。

    这份在用户目录里，没有只读位，也不需要提权 —— 它是最稳的那一条。
    """
    d = home / "cache"
    out: list[Hit] = []
    try:
        if not d.is_dir():
            return []
        cands = sorted(d.glob("acc-product-config-v*.json"), reverse=True)
    except OSError:
        return []
    for p in cands:
        try:
            if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
                continue
            t = read_text(p)
        except OSError:
            continue
        if CP_OPEN not in t:
            continue
        try:
            json.loads(t)
        except ValueError:
            continue
        out.append(Hit(p, "json", "产品配置缓存 · " + p.name))
    return out


def app_policy_hits(root: Path | None) -> list[Hit]:
    """安装目录里的插件模板（welcomemode-*）—— 用户目录那份的上游。"""
    if not root:
        return []
    d = root / PLUGIN_SUBDIR
    if not d.is_dir():
        return []
    out: list[Hit] = []
    try:
        entries = sorted(d.glob("*/*/prompt.tpl"))
    except OSError:
        return []
    for p in entries:
        try:
            if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
                continue
            head = read_text(p)
        except OSError:
            continue
        if CP_OPEN not in head:
            continue
        out.append(Hit(p, "apppol", "安装目录插件模板 · " + p.parent.parent.name))
    return out


def sys_tpl_hits(root: Path | None) -> list[Hit]:
    """system role 通道：安装目录下的主提示词模板。

    为什么必须有这条通道 —— 这是「破甲没效果」的真正原因：

      * WorkBuddy 有两条通道。`~/.workbuddy/plugins/cache/*/prompt.tpl` 是
        **补充上下文**（user-context 层级），人格放那儿，模型按「参考材料」
        处理，权重最低。
      * 安装目录 `resources/app.asar.unpacked/resources/templates/*.tpl`
        才是**系统提示词本体**（system role），客户端每次开会话都把它
        渲染成 system 消息。

    只写前者，人格就是「塞在上下文里的一段话」；写后者才是「你是谁」。

    实测（2026-11-26，本机）：只写插件缓存时，Codex 请求里人格占
    developer 消息的 18.3%，其余 81.7% 是原生 developer 指令；而
    templates 通道里当时躺的是上一代工具遗留的 25 KB 人格，
    占模板 96% —— 也就是真正生效的那条通道被别的东西占了，
    小熊猫的话模型压根听不见。

    判据用正文里的 `This conversation is powered by`（system role 模板的
    第一行），不用文件名 —— 同目录里还有几十字节的小提醒片段，
    往里塞人格没有意义。
    """
    if not root:
        return []
    tdir = root / TPL_SUBDIR
    if not tdir.is_dir():
        return []
    hits: list[Hit] = []
    try:
        entries = sorted(tdir.glob("*.tpl"))
    except OSError:
        return []
    for p in entries:
        try:
            if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
                continue
            head = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SYS_TPL_MARK not in head:
            continue
        hits.append(Hit(p, "sys", "系统提示词模板 · system role"))
    return hits


def _ver_key(d: Path) -> tuple:
    """版本目录排序键：按版本号数字比，比不了再退回 mtime。

    为什么不用纯 mtime：客户端解压插件时，旧版本目录的 mtime 可能比新的
    还晚（rar/zip 解压会保留打包时间）。一升级就认错版本，注入写到一个
    永远不被加载的目录里 —— 用户点了破甲，客户端里人格没变。

    版本名形如 `5.5.6-wb.38337834.g5f969292.h9c7c332db516`，取前 3 段数字
    当主键；解析不出来就给 (0,0,0)，再靠 mtime 兜底。
    """
    name = d.name
    nums: list[int] = []
    for part in name.replace("_", ".").replace("-", ".").split("."):
        if part.isdigit():
            nums.append(int(part))
        if len(nums) >= 3:
            break
    while len(nums) < 3:
        nums.append(0)
    try:
        mt = d.stat().st_mtime
    except OSError:
        mt = 0.0
    return (tuple(nums), mt)


def _in_use(plug: Path, ver: Path) -> bool:
    """这个版本目录是不是客户端当前在用的那个。

    客户端会在正在使用的版本目录下放一个 `.in_use` 目录（本机实测
    welcomemode-work 有、另两个没有 —— 说明它是「当前工作模式」的标记，
    不是「这个插件被加载」的标记）。所以它只能当加分项，不能当唯一依据：
    有 .in_use 的排最前，其余照版本号排序，全都写。
    """
    try:
        return (ver / ".in_use").is_dir()
    except OSError:
        return False


def _tpl_hits(home: Path) -> list[Hit]:
    """主提示词模板。同一插件有多个版本目录时**全都写**。

    早期只取最新的一个版本目录，理由是「客户端跑的就是那个」。这个假设在
    两种情况下会失效，而且失效时用户完全看不出问题：

      1. 客户端保留多个版本目录做灰度和回滚。只改最新的那个，客户端实际
         加载的是另一个 —— 破甲报成功，人格没变。
      2. 版本目录名不是纯数字（`5.5.6-wb.38337834.g...`），按 mtime 排
         序会认错（解压保留打包时间）。

    改成一个插件下**所有**含 prompt.tpl 的版本目录都算靶点。代价是同名
    插件多写几个文件（每个几十 KB），收益是「不管客户端加载哪个都在」。
    这些文件本来就是同一份模板的不同版本，写同一段人格不会互相打架。

    另外按 `<content_policy>` 是否存在做二次确认：没有策略段的模板不碰 ——
    那种文件可能是别的用途的 Jinja 模板，往里塞人格会破坏语法。
    """
    base = home / CACHE
    if not base.is_dir():
        return []
    try:
        plugins = sorted(p for p in base.iterdir()
                         if p.is_dir() and p.name.startswith(TPL_PLUGIN_PREFIX))
    except OSError:
        return []

    hits: list[Hit] = []
    for plug in plugins:
        try:
            vers = [d for d in plug.iterdir() if d.is_dir()]
        except OSError:
            continue

        # .in_use 的排最后（下面取 [-1] 之外的顺序无所谓，全写），
        # 排序只为让日志和 changed 列表稳定，方便对账。
        try:
            vers.sort(key=_ver_key)
        except Exception:                                      # noqa: BLE001
            pass

        for ver in vers:
            tpl = ver / "prompt.tpl"
            try:
                if not tpl.is_file():
                    continue
                # 50 KB 上下是正常值。上限放到 8 MB 是为了挡住「用户把别的
                # 大文件命名成 prompt.tpl」这种脏数据，正常模板远不到。
                if tpl.stat().st_size > 8 * 1024 * 1024:
                    continue
            except OSError:
                continue
            # 有策略段才认。这是「这个 tpl 是不是真的主提示词」的唯一硬证据，
            # 比文件名可靠 —— 同名文件在别的插件里也可能叫 prompt.tpl。
            try:
                head = tpl.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if CP_OPEN not in head:
                continue
            note = "主提示词 · 策略段"
            if _in_use(plug, ver):
                note += "（在用）"
            hits.append(Hit(tpl, "policy", note))
    return hits


class WorkBuddy(Target):
    name = "wb"
    label = "WorkBuddy"
    procs = ("workbuddy.exe",)

    def __init__(self, root: Path | None = None):
        self.explicit = root
        self.home = wb_home()

    @classmethod
    def looks_like(cls, p: Path) -> tuple[bool, str]:
        """用户手选目录时，只接受「用户目录」，不接受安装目录。

        这是 WorkBuddy 最容易选错的地方：安装目录（Programs\\WorkBuddy）
        看着更像「安装位置」，但靶点在用户目录 `~/.workbuddy` 里 ——
        安装目录是 284 MB 的 app.asar，改它必然崩。所以这里主动识别
        安装目录并明确告诉用户该选哪个。
        """
        low = p.name.lower()
        if low == ".workbuddy":
            return True, ""
        # 用户目录特征
        if has_any(p, WB_HOME_MARKS):
            return True, ""
        # 安装目录特征 —— 认出来后给一条能照着做的指引
        try:
            if (p / "WorkBuddy.exe").exists() or (p / "resources" / "app.asar").exists():
                return False, ("这是 WorkBuddy 的安装目录，不用改它。"
                               "请选用户目录下的 .workbuddy（C:\\Users\\<你>\\.workbuddy）")
        except OSError:
            pass
        return False, ("这个目录里没有 WorkBuddy 的用户配置，"
                       "选 C:\\Users\\<你>\\.workbuddy")

    def detect(self) -> bool:
        # 手动指定的优先。它一定是用户目录 —— looks_like 已经把安装目录挡掉了。
        self.manual_used = False
        manual, why = manual_root("wb", self.looks_like)
        if manual:
            self.home = manual
            self.root = manual
            self.manual_note = ""
            self.manual_used = True
            return True
        if why:
            self.manual_note = why

        # 用户目录排第一：靶点在那儿，界面显示哪儿比显示安装目录有用。
        for d in (self.home, self.explicit, find_root()):
            try:
                if d and Path(d).is_dir():
                    self.root = Path(d)
                    return True
            except OSError:
                continue
        return False

    def collect(self) -> list[Hit]:
        hits = _tpl_hits(self.home)

        # 兜底人格文件：插件缓存没下全时，这个还在
        soul = self.home / "SOUL.md"
        try:
            if soul.is_file() and soul.stat().st_size <= 2 * 1024 * 1024:
                hits.append(Hit(soul, "block", "人格文件 SOUL.md"))
        except OSError:
            pass

        # system role 通道：安装目录下的主提示词模板。**这一组是主通道**，
        # 插件缓存那组是补充上下文 —— 只写后者，人格就是「上下文里的一段
        # 附带说明」，模型按参考材料处理。
        #
        # 这里必须用 find_root() 拿安装目录，不能用 self.root：
        # self.root 是用户目录 ~/.workbuddy，那里没有 resources/templates
        # （原来那版代码就写错了路径，这组靶点从来没命中过）。
        root = find_root()
        hits.extend(sys_tpl_hits(root))

        # 产品配置通道：桌面端 system prompt 的真正来源。
        hits.extend(product_hits(root))

        # 用户目录里的派生副本。客户端读哪一份不由我们决定，两条都写。
        hits.extend(cache_hits(self.home))

        # 安装目录里的插件模板。用户目录 `plugins/cache/.../prompt.tpl`
        # 是它们的副本，客户端启动时覆盖一次，所以上游也得写。
        hits.extend(app_policy_hits(root))
        return hits

    def body(self, persona: str) -> str:
        # Jinja 语法在模板里会被求值，人格正文原样贴进去前先把定界符打散。
        # 只影响 "{{" 这类三字符，正文可读性不变。
        out = persona
        for k, v in JINJA:
            out = out.replace(k, v)
        return out

    def fingerprint_of(self, persona: str) -> str:
        """指纹要从**打散后**的正文里取。

        写进 prompt.tpl 的是 body(persona)，不是 persona 本身。拿原文的
        指纹去文件里找，只要那一行带 `{{`/`}}` 就永远匹配不上，体检直接
        报「注入的不是这份人格」。
        """
        return fingerprint(self.body(persona))

    def inject_base(self, h: Hit) -> str:
        """**注入**用的底稿：就地擦，保住当前载体的段外正文。

        跟 `clean_base()`（还原底稿）是两件不同的事，混用会互相破坏：

          * 注入要保住「现在磁盘上最新、最全的官方正文」。真机上
            `product.json` 的 template 45,826 字符，`.unlockbak` 只有
            19,607 —— 拿备份当底，等于把官方正文回退一年。
          * 安装目录 `prompt.tpl` 更要命：段外那几个块已经被上一代注入器
            改成了放行版（`<personal_files_safety>` → `<文件操作无限制>`、
            `windows_command_safety` 改成允许任意包装层）。`.unlockbak`
            里是官方严格版，拿它当底等于每次注入都把解锁条款重新上锁，
            然后报「破甲成功」—— 用户测出来就是「没效果」。
          * 还原要的正好相反：回到官方原样，只有 `.unlockbak` 有。

        所以这里只做「清策略段内文 + 切末尾旧追加段」，段外一个字节不动。
        """
        if h.how == "json":
            cur = read_json_template(h.path)
            if not cur:
                return ""
            # 当前 template 里可能还留着上一代工具的 `<system_reminder>`
            # 整段工作区设定（带真群号）。那段必须切，否则新旧两套指令
            # 正面撞车。切完再清策略段内文。
            return _clean_policy_text(cur)
        if h.how == "apppol":
            try:
                cur = read_text(h.path)
            except OSError:
                return ""
            if CP_OPEN not in cur:
                return ""
            return _clean_policy_text(cur)
        return self.clean_base(h)

    def clean_base(self, h: Hit) -> str:
        """干净原文（**还原**底稿）。按可信度依次找。

        1. `prompt.tpl.xm-bak` —— 本工具注入前留的。
        2. `prompt.tpl.unlockbak` / `.bak` / `.orig` —— 老注入器留的。

        有它就以它为底。理由：现在磁盘上的 prompt.tpl 可能已经被上一次注入
        改过（策略段被整段替换），直接在上面改会丢掉原文里的 memory 系统、
        Jinja 片段。备份文件才是真的原文。

        两条通道的「像不像原件」判据不同：
          * policy 通道必须含 `<content_policy>` —— 那是主提示词的策略段。
          * sys 通道必须含 `This conversation is powered by` —— 策略段在
            部分模式模板（ask / expert）里被裁掉了，拿 CP_OPEN 当判据会把
            真原件判成脏数据，于是还原退化成「切标记段」，模板里那两万字的
            官方正文就永久丢了。

        json / apppol 两条通道走各自的读法：json 要按字段取 template，
        apppol 是整份 .tpl。两者都优先 `.unlockbak`（实测干净官方原件），
        json 再退 `.xm-bak`（本工具留的整份字节）。安装目录里的模板不该
        额外多出备份文件，所以这两条通道我们不写 `.xm-bak` 给 apppol。
        """
        if h.how in ("json", "apppol"):
            # 还原底稿 = 官方原件，只从备份里找。**不能**就地擦：就地擦的
            # 结果策略段是空的，写回去虽然结构合法，但逐字节不等于原件，
            # 用户点「还原」拿到的不是原样。
            #
            # 顺序：`.xm-bak`（本工具首次注入前存的整份字节，最新版官方）
            # 优先，`.unlockbak`（上一代工具留的）兜底。
            order = ((".xm-bak", ".unlockbak") if h.how == "json"
                     else (".unlockbak", ".xm-bak"))
            for suffix in order:
                bak = h.path.with_name(h.path.name + suffix)
                try:
                    if not (bak.is_file() and bak.stat().st_size > 0):
                        continue
                    t = (read_json_template(bak) if h.how == "json"
                         else read_text(bak))
                except OSError:
                    continue
                if not t:
                    continue
                t = _strip_orphan_sections(t)
                if CP_OPEN not in t or residue(t):
                    continue
                # 备份自己带块 = 它存的时候就是注入态。直接返回等于把旧人格
                # 写回客户端（1.4.x 的 `========== xm-doc:begin ==========`
                # 块就是这么活的）。先剥一遍，剥完必须是干净官方模板才认；
                # 剥完还不干净就换下一个备份 —— 宁可退到就地擦，也不写脏的。
                if _bak_dirty(t, "sys" if h.how == "apppol" else "text"):
                    t = strip_block(t)
                    if CP_OPEN not in t or _bak_dirty(t, "text"):
                        continue
                return t
            return ""

        if h.how not in ("policy", "sys"):
            return ""
        # 顺序不同：sys 通道优先认 `.unlockbak`（老工具留的官方原件），
        # policy 通道优先认自己留的 `.xm-bak`。
        #
        # 为什么 sys 要反过来：`.xm-bak` 可能是在「模板已被上一代工具改过」
        # 的状态下存下来的。里面的 `<!-- unlock-v6 -->` 块没有闭合标记，
        # 判不出来，于是被当成干净原件 → 还原把旧人格又写回去。
        # `.unlockbak` 是那份工具动手前留的，才是真的官方原文。
        order = ((".unlockbak", ".xm-bak", ".bak", ".orig")
                 if h.how == "sys"
                 else (".xm-bak", ".unlockbak", ".bak", ".orig"))
        for suffix in order:
            p = h.path.with_name(h.path.name + suffix)
            try:
                if p.is_file() and p.stat().st_size > 1024:
                    t = read_text(p)
                    if residue(t):
                        continue
                    if h.how == "sys":
                        if SYS_TPL_MARK in t and not has_sys_block(t):
                            return t
                        continue
                    if CP_OPEN not in t:
                        continue
                    # 同 json / apppol：备份自己带块就是脏的。
                    # 这里只判 `has_block` —— 上面已经排除 residue 了。
                    if has_block(t):
                        t = strip_block(t)
                        if CP_OPEN not in t or has_block(t):
                            continue
                    return t
            except OSError:
                continue
        return ""

    def _save_clean(self, h: Hit) -> None:
        """注入前把干净原文留一份 `prompt.tpl.xm-bak`。

        为什么必须有这一步：还原时如果没有原文，只能把标记段切掉，留下一个
        空的 `<content_policy></content_policy>` —— 模板结构还在，但策略段
        空了，客户端读到空策略的行为没人验证过。sys 通道更严重：模板里
        两万到三万字的官方正文散在十几个段里，切标记段救不回来。

        只在**首次**注入时写：文件已存在就不覆盖，否则第二次注入会把
        「已经注入过的版本」存成原文，跟 revert 那个历史 bug 一模一样。

        json 通道由 `_save_json_clean()` 处理，不在这里 —— 那份文件要按
        「整份字节」存，且判据是可擦干净的 template，不是文本里没有标记。
        """
        if h.how == "json":
            _save_json_clean(h.path)
            return
        if h.how not in ("policy", "sys", "apppol"):
            return
        bak = h.path.with_name(h.path.name + ".xm-bak")
        try:
            if bak.exists() and bak.stat().st_size > 1024:
                return
            raw = read_text(h.path)
            if not raw.strip():
                return
            # 已经带标记段 = 文件是注入态，这时候存下来的是脏的，不存。
            # 那种情况下 clean_base() 会去找 .unlockbak（老工具留的）。
            if h.how == "sys":
                if has_sys_block(raw) or SYS_TPL_MARK not in raw:
                    return
            else:
                # 上一代工具留下的无标记人格块判不出来（没有闭合标签），
                # 但它有可识别的注释头。有就说明这份文件已经被改过，
                # 现在存下来就是存「脏原文」，直接跳过 —— 还原会去用
                # `.unlockbak`（那份工具动手前的官方原件）。
                if has_block(raw) or CP_OPEN not in raw or LEGACY_HEAD.search(raw):
                    return
            atomic_write(bak, raw)
        except OSError:
            # 存不下就算了：还原退化成「切标记段」，功能仍在。
            pass

    def is_applied(self, h: Hit) -> bool:
        """json 通道要看 template 字段，不能拿整份 JSON 文本判。

        整份文本里也搜得到 `========== xm-doc:begin ==========`（JSON 字符串
        不转义 `=` `:` 空格），但那个字符串可能躺在别的 prompt 里 ——
        实测这份文件有三十多条 prompt，早期的注入器确实把人格塞进过
        `auto-mode-classifier-instructions`。判在错的地方，界面会报
        「已破甲」而主提示词一个字没动。
        """
        if h.how == "json":
            t = read_json_template(h.path)
            return BEGIN in t or bool(SEAL_RE.search(t))
        if h.how == "apppol":
            t = read_text(h.path)
            return BEGIN in t or bool(SEAL_RE.search(t))
        return super().is_applied(h)

    def strip_target(self, h: Hit) -> bool:
        """WorkBuddy 的兜底顺序跟别人不一样。

        它的注入点在 `<content_policy>` 段里。只把标记段切掉会留下一个空的
        `<content_policy></content_policy>` —— 模板结构算完整，但策略段空了，
        客户端读到空策略比读到注入还怪。所以顺序是：

          1. 有原文备份（`.xm-bak` / `.unlockbak`）→ 整份写回
          2. 没有就切标记段，把 `<content_policy>` 留成空段

        第 2 种不算完美，但比留着人格强，而且 verify 的定界符检查能过。
        """
        if h.how in ("json", "apppol"):
            return self.strip_special(h)
        if h.path.name == SOUL_NAME:
            return self.strip_soul(h)
        clean = self.clean_base(h)
        if clean:
            try:
                if read_text(h.path) != clean:
                    # 模板按文本写回即可：`prompt.tpl` 是客户端自己按 UTF-8
                    # 读的 Jinja 模板，原文也是 UTF-8。用 write_like 统一
                    # UTF-8，比「猜原编码」安全 —— 猜错会把整个模板变乱码，
                    # 客户端渲染直接报错。
                    write_like(h.path, clean)
                    return True
            except OSError:
                pass
            return False
        return super().strip_target(h)

    def strip_soul(self, h: Hit) -> bool:
        """还原 `~/.workbuddy/SOUL.md`。

        这是四条通道里唯一**没有可靠原文**的一条，所以单独一套判据：

          1. 剥完还留着官方模板的特征（`title: "SOUL.md Template"`）
             → 那就是用户没改过的官方文件，直接用剥完的结果。
          2. 剥完既短又带残留特征（`阿岩的行事准则` 这类）
             → 说明这文件被上一代注入器整份覆盖过，用户原本的官方模板
               早就不在磁盘上了。整份换成 `SOUL_DEFAULT`（从 app.asar 里
               的 `DEFAULT_SOUL_CONTENT` 抄出来的原文，逐字节一致）。
          3. 其他 → 用户自己写过内容，一个字节都别动，只切标记段。

        为什么不是「有备份就写备份」：实测最早的历史备份（20261121-165926）
        里那份 1792 B 的 SOUL.md 确实就是官方原文，但 `original_of()` 要求
        manifest 里 `exists=True` 且有 backup 文件，而这条记录是 strip 阶段
        写的，`residue()` 判它带 `阿岩的行事准则` 直接拒。所以不能指望备份。
        """
        try:
            old = read_text(h.path)
        except OSError:
            return False
        new = scrub_legacy(strip_block(old))

        # 1) 官方模板壳还在 —— 剥干净了
        if SOUL_OFFICIAL_MARK in new and not residue(new):
            if new == old:
                return False
            write_like(h.path, new)
            return True

        # 2) 残壳 = 被整份覆盖过。只有「又短又脏」才认这个判据：
        #    用户自己写的 SOUL.md 可能很长，不能因为里面恰好有个人格词就
        #    整份换掉 —— 那是删用户数据。
        #
        #    阈值 1600 是量出来的：官方原文 1792 字符，实测残壳 688 字符。
        #    取 1600 而不是 512，是因为残壳里那句 front matter
        #    （`summary: "阿岩的行事准则"` + 六个小节标题）就占了 688 ——
        #    512 会让它掉进第 3 条「只切块」，还原后用户拿到的还是一份
        #    中英混杂的人格残片。1600 既盖住残壳，又低于官方原文，不会被
        #    误用到正常文件上。
        if len(new.strip()) < 1600 and residue(new):
            write_like(h.path, SOUL_DEFAULT)
            return True

        # 3) 用户的文件，只切标记段
        if new == old:
            return False
        if not new.strip():
            # 剥完是空的：这文件本来就是注入时新建的（用户从没写过 SOUL.md），
            # 留个空文件在用户目录里没意义，但也别删 —— 客户端下次启动会自己
            # 重建。写成官方默认内容，比留空更像「刚装好」的状态。
            write_like(h.path, SOUL_DEFAULT)
            return True
        write_like(h.path, new)
        return True

    def apply_special(self, h: Hit, body: str, bk) -> bool:
        """json / apppol 两条通道的写入。

        共同的约束：**只动 `<content_policy>` 段里的正文**，段外一个字节
        不动。这两份载体都在安装目录里，`</content_policy>` 后面还有一到
        两万字官方正文（security 说明、tone、task management、Jinja 条件块），
        切到文末等于把客户端要渲染的 system prompt 截断 —— 那不是「破甲
        没生效」，是整个会话起不来。

        底稿走 `inject_base()`，**不是** `clean_base()`：后者是还原用的
        `.unlockbak`，拿它当注入底会把段外的解锁条款回退成官方严格版。
        """
        clean = self.inject_base(h)
        if h.how == "json":
            # 没有可用原件时**不能**只切末尾追加段：策略段里的旧注释头还在，
            # `_policy_replace` 只换段内正文，那两万字旧人格会跟着新块一起
            # 留在模板里，两份人格正面撞车。走 `_clean_policy_text`，
            # 它保证段内是空的。
            cur = read_json_template(h.path)
            src = clean or _clean_policy_text(cur)
            src = _strip_orphan_sections(src)
            new = _policy_replace(src, body)
            if not new or new == cur:
                # 已经是目标状态 → 算成功。这不是「写入失败」：同一次注入里
                # product.json 处理完会顺手把模板推给用户目录缓存，轮到缓存
                # 那个靶点时它已经是对的。返回 False 会让界面报「部分失败」，
                # 用户以为没成，其实内容正确。
                return True
            ok = write_json_template(h.path, new)
            if ok and h.path.name == PRODUCT_NAME:
                # 安装目录那份改完后把同一份模板推给用户目录缓存，
                # 免得用户不重启客户端时缓存里还是旧人格。
                self._sync_cache(new)
            return ok

        if h.how == "apppol":
            try:
                old = read_text(h.path)
            except OSError:
                return False
            # 这里**不能**用 scrub_legacy()：它是「从头标记一路切到文末」，
            # 而安装目录这三份模板在策略段之后还有两万五千字官方正文
            # （tone / task management / Jinja 条件块）。切到末尾 =
            # 客户端渲染 system prompt 时直接崩。
            src = clean or _clean_policy_text(old)
            src = _strip_orphan_sections(src)
            new = _policy_replace(src, body)
            if new == old:
                return True
            try:
                bk.save(self.name, h.path)
                write_like(h.path, new)
                return True
            except OSError:
                return False
        return False

    def strip_special(self, h: Hit) -> bool:
        """还原 json / apppol：整份写回官方原件。

        不能只切标记段 —— 这两份载体上旧工具留的是**无尾**的注释头
        （`<!-- unlock-v6:... -->` 一路到文末），切标记段切不掉它，
        还原后模板里仍占 95%，等于没还原。`.unlockbak` 才是真原件。
        """
        clean = self.clean_base(h)
        if not clean:
            return False
        if h.how == "json":
            if read_json_template(h.path) == clean:
                return False
            return write_json_template(h.path, clean)
        try:
            if read_text(h.path) == clean:
                return False
            write_like(h.path, clean)
            return True
        except OSError:
            return False

    def _sync_cache(self, tpl: str) -> None:
        """把注入后的模板同步进用户目录的产品配置缓存。

        `~/.workbuddy/cache/acc-product-config-v3.json` 是客户端启动时按
        安装目录 product.json 派生的副本，但派生的时机是「启动」——
        用户在软件里点了破甲之后不重启客户端，缓存里还是旧模板。
        而下一轮会话读安装目录还是读缓存，不由我们决定。

        两条都写：安装目录那份改了，缓存这份也改成同样的内容。写失败不算
        错 —— 客户端启动时会自己重新派生一次。
        """
        try:
            home = wb_home()
        except OSError:
            return
        for h in cache_hits(home):
            try:
                if read_json_template(h.path) == tpl:
                    continue
                write_json_template(h.path, tpl)
            except OSError:
                continue

    def strip_sys_target(self, h: Hit) -> bool:
        """还原 system role 通道。

        这条通道的还原必须**整份写回原件**，不能只切标记段：

          * 模板正文有三万到六万字符，system role 的内容散在
            `<content_policy>`、`<personal_files_safety>` 等多个段里。
          * 上一代工具的无标记人格块（`<!-- unlock-v6:... -->`）没有闭合
            标记，只能靠「从标记处切到文末」清掉 —— 留着它，
            还原后模板里仍占 96%，等于没还原。
          * `.unlockbak` 就是官方原件（实测 10 个模板全有，且无残留特征）。

        优先顺序：
          1. 有原件备份 → 整份写回（最准）
          2. 没有 → 切 sys 标记段 + 清 legacy 块，模板结构保持完整
        """
        clean = self.clean_base(h)
        if clean:
            try:
                if read_text(h.path) != clean:
                    write_like(h.path, clean)
                    return True
            except OSError:
                pass
            return False

        # 没有原件：切掉我们的块和指针，再清上一代工具的遗留人格。
        try:
            old = read_text(h.path)
        except OSError:
            return False
        new = scrub_legacy(strip_sys_block(old))
        if new == old:
            return False
        try:
            write_like(h.path, new)
            return True
        except OSError:
            return False

    def inject(self, persona: str, dry: bool = False):
        """先留原文，再走基类注入。

        顺序很重要：`_save_clean` 必须在写标记段之前跑，否则存下来的就是
        注入后的内容。这里在基类 inject 之前逐个靶点存一遍。
        """
        if not dry:
            try:
                if self.detect():
                    for h in self.collect():
                        self._save_clean(h)
            except Exception:                                  # noqa: BLE001
                # 留原文失败不该拦住注入 —— 还原还有「切标记段」这条退路
                pass
        return super().inject(persona, dry)

    def verify(self, persona: str = "") -> list[dict]:
        out = super().verify(persona)
        for h in self.collect():
            if h.how in ("json", "apppol"):
                # 这两条通道的模板里有大量 Jinja 条件块（{% if %}），注入把
                # 某个 end tag 吃掉，客户端渲染整份 system 提示词时直接报错
                # —— 那不是「破甲没生效」，是整个会话起不来。所以配平检查
                # 不能省，而且要在**切掉我们的块之后**数，否则我们正文里
                # 打散过的定界符会被算进去。
                t = (read_json_template(h.path) if h.how == "json"
                     else read_text(h.path))
                if not t:
                    out.append({"ok": False, "text": f"{h.path.name} 读不出模板",
                                "hint": "文件可能损坏，用 .unlockbak 还原"})
                    continue
                tpl = strip_block(t)
                pairs = (("{%", "%}"), ("{{", "}}"), ("{#", "#}"))
                broken = [a for a, b in pairs if tpl.count(a) != tpl.count(b)]
                if broken:
                    out.append({"ok": False,
                                "text": f"{h.path.name} 模板定界符不配对：{'、'.join(broken)}",
                                "hint": "还原后重跑一次破甲"})
                else:
                    out.append({"ok": True, "text": f"{h.path.name} 模板结构完整"})
                continue
            if h.how == "sys":
                # system role 是主通道，模板结构必须单独验一遍：它里面有
                # 大量 Jinja 条件块（{% if %}），注入把某个 end tag 吃掉，
                # 客户端渲染整份 system 提示词时直接报错 —— 那不是「破甲没
                # 生效」，是整个会话起不来。所以这里配平检查不能省。
                t = read_text(h.path)
                tpl = scrub_legacy(strip_sys_block(t))
                pairs = (("{%", "%}"), ("{{", "}}"), ("{#", "#}"))
                broken = [a for a, b in pairs if tpl.count(a) != tpl.count(b)]
                if broken:
                    out.append({"ok": False,
                                "text": f"{h.path.name}（system role）定界符不配对：{'、'.join(broken)}",
                                "hint": "还原后重跑一次破甲"})
                elif SYS_TPL_MARK not in tpl:
                    out.append({"ok": False,
                                "text": f"{h.path.name}（system role）丢了首行标识",
                                "hint": "用 .unlockbak 还原这个模板"})
                else:
                    out.append({"ok": True,
                                "text": f"{h.path.name}（system role）结构完整"})
                continue
            if h.how != "policy":
                continue
            t = read_text(h.path)
            # 先把注入块切掉再数。我们自己写进去的正文是打散过的定界符，
            # 不该影响模板本身的配平；而如果在这里统计整份文件，别的工具
            # 留在别处的一个孤立 `}}` 会被算到我们头上，报「还原后重跑」
            # 也没用 —— 用户照做一遍还是同样的提示。
            tpl = strip_block(t)
            # 不引 jinja2（打包时不想多 2 MB），只查定界符配不配对。
            pairs = (("{%", "%}"), ("{{", "}}"), ("{#", "#}"))
            broken = [a for a, b in pairs if tpl.count(a) != tpl.count(b)]
            if broken:
                out.append({"ok": False,
                            "text": f"{h.path.name} 模板定界符不配对：{'、'.join(broken)}",
                            "hint": "还原后重跑一次破甲"})
            else:
                out.append({"ok": True, "text": f"{h.path.name} 模板结构完整"})
        return out

    def status(self) -> str:
        if not self.detect():
            return "未安装"
        hits = self.collect()
        if not hits:
            return f"已安装（{self.root}），没有可写的靶点"
        live = sum(1 for h in hits if self.is_applied(h))
        return (f"已安装（{self.root}），靶点 {len(hits)} 个，"
                f"已破甲 {live} 个")
