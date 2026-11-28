"""DSH（DeepSeek Harness）适配器。

靶点从它自己的文档里读出来（app.asar 里的说明文本）：

    one durable baseline message with the user-global chain —
    `$DSH_HOME/AGENTS.md`, then `<agentsHome>/AGENTS.md` —
    followed by the project chain

    `$DSH_HOME`, or `~/.dsh`, through `@deepseek-ai/dsh-home-paths`
    共享 agents 根目录同样如此解析：`$DSH_AGENTS_HOME` 或 `~/.agents`

也就是说用户全局链是两级：

    ~/.dsh/AGENTS.md        （$DSH_HOME 可覆盖）
    ~/.agents/AGENTS.md     （$DSH_AGENTS_HOME 可覆盖）

再往后才是项目级 AGENTS.md / CLAUDE.md —— 那属于项目自己，不碰。

安装目录的 Markdown 一律不当靶点：桌面版把提示词全压在 209 MB 的 app.asar 里，
装出来的只有 Electron 外壳，没有明文。
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import HOME, Hit, Target, has_any, manual_root

HOME_ENV = "DSH_HOME"
AGENTS_ENV = "DSH_AGENTS_HOME"

#: 判断「这个目录是不是 DSH 的家」用的特征名。AGENTS.md 自己也算 ——
#: 用户可能就是先把目录建好、把全局指令写进去了。
#: 不列 logs / cache / sessions —— 这些名字哪里都有，拿它们当特征
#: 等于不校验（C:\Windows 底下全有）。
DSH_MARKS = ("AGENTS.md", "config.toml", "history.jsonl", "dsh.db")

#: 桌面版真实安装目录（装出来叫 "DeepSeek Harness"，不是 "DSH Desktop"）
INSTALL_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DeepSeek Harness",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DSH Desktop",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "dsh-desktop",
    Path("C:/Program Files/DeepSeek Harness"),
]


def dsh_home() -> Path:
    env = os.environ.get(HOME_ENV)
    return Path(env) if env else (HOME / ".dsh")


def agents_home() -> Path:
    env = os.environ.get(AGENTS_ENV)
    return Path(env) if env else (HOME / ".agents")


class DSH(Target):
    name = "dsh"
    label = "DSH"
    procs = ("dsh.exe", "dsh-desktop.exe", "dsh desktop.exe",
             "deepseek harness.exe")

    def __init__(self, root: Path | None = None):
        self.explicit = root
        #: 手动指定的目录（pick_dir 记下来的）。有它就以它为 DSH_HOME ——
        #: 不然 detect() 会认「装了」，collect() 却仍然写 ~/.dsh，
        #: 两处对不上，用户指定完目录发现根本没写进去。
        self.manual: Path | None = None

    def _home(self) -> Path:
        return self.manual if self.manual else dsh_home()

    @classmethod
    def looks_like(cls, p: Path) -> tuple[bool, str]:
        """DSH 的 home 目录，默认 `~/.dsh`。

        名字对上就放行 —— 空目录也是合法的（客户端没启动过时就是这样，
        首次启动会自己往里写）。名字对不上则要有点内容才认，避免用户
        随手指一个空文件夹，界面显示「已安装」但一个靶点都没有。
        """
        low = p.name.lower()
        if low in (".dsh", ".agents"):
            return True, ""
        if not has_any(p, DSH_MARKS):
            return False, ("这个目录里没有 DSH 的东西（AGENTS.md 等），"
                           "选用户目录下的 .dsh 文件夹")
        return True, ""

    def detect(self) -> bool:
        self.manual_used = False
        manual, why = manual_root("dsh", self.looks_like)
        if manual:
            self.manual = manual
            self.manual_note = ""
            self.root = manual
            self.manual_used = True
            return True
        self.manual = None
        if why:
            self.manual_note = why
        if self.explicit and self.explicit.is_dir():
            self.root = self.explicit
            return True
        # ~/.dsh 存在但里面空空的：客户端没启动过会这样，仍然算装了 ——
        # 但要真的一点东西都没有，且安装目录也找不到，就说明那只是个
        # 同名空目录（别的工具建的、或者用户手动 mkdir 的），不当已安装。
        home = dsh_home()
        if home.is_dir() and (has_any(home, DSH_MARKS) or self._install_dir()):
            self.root = home
            return True
        d = self._install_dir()
        if d:
            self.root = d
            return True
        return False

    @staticmethod
    def _install_dir() -> Path | None:
        for d in INSTALL_DIRS:
            try:
                if d and d.is_dir():
                    return d
            except OSError:
                continue
        return None

    def collect(self) -> list[Hit]:
        hits: list[Hit] = []
        h1 = self._home() / "AGENTS.md"
        h2 = agents_home() / "AGENTS.md"

        # 第一级链，官方文档明写的。不存在也建 —— 这是主靶点。
        #
        # **where 必须是 end。** 1.4.0 用过 top，理由跟 ZCode 一样（文件本就在
        # system 末尾，放头部离读取动作最近）。实测否掉了这个理由：标记行出现在
        # 正文之前时，模型判定整段是「内部配置」，问它复述提示词回 34 字拒绝；
        # 同一份正文把标记挪到末尾 → 3181 字全文。详见 core/zcode.py:collect。
        #
        # guard 不启用。理由见 core/base.py 的 LEGACY_GUARDS 注释 ——
        # 块外那段「这不是越狱」的声明本身就被模型当成越狱特征。
        hits.append(Hit(h1, "block", "用户全局 AGENTS.md（$DSH_HOME）"))

        # 第二级链：只在目录已经存在时才写，没装的机器不去动它
        if h2.exists() or agents_home().is_dir():
            hits.append(Hit(h2, "block", "共享 agents 根 AGENTS.md"))

        # 去重
        seen, out = set(), []
        for h in hits:
            k = str(h.path).lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(h)
        return out
