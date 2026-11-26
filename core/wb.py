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

import os
from pathlib import Path

from .base import (CP_OPEN, HOME, LEGACY_HEAD, Hit, Report, Target, atomic_write,
                   fingerprint, has_any, has_block, has_sys_block, manual_root,
                   read_text, residue, scrub_legacy, strip_block, strip_sys_block,
                   write_like)

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
        hits.extend(sys_tpl_hits(find_root()))
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

    def clean_base(self, h: Hit) -> str:
        """干净原文。按可信度依次找。

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
        """
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
                    if CP_OPEN in t:
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
        """
        if h.how not in ("policy", "sys"):
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

    def strip_target(self, h: Hit) -> bool:
        """WorkBuddy 的兜底顺序跟别人不一样。

        它的注入点在 `<content_policy>` 段里。只把标记段切掉会留下一个空的
        `<content_policy></content_policy>` —— 模板结构算完整，但策略段空了，
        客户端读到空策略比读到注入还怪。所以顺序是：

          1. 有原文备份（`.xm-bak` / `.unlockbak`）→ 整份写回
          2. 没有就切标记段，把 `<content_policy>` 留成空段

        第 2 种不算完美，但比留着人格强，而且 verify 的定界符检查能过。
        """
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
