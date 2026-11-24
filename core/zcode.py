"""ZCode（Z.ai 的 coding agent）适配器。

靶点在用户目录：`~/.zcode/AGENTS.md`。

这条不是猜的 —— ZCode 3.18.2 的 app.asar 里能直接读到：

    function getUserInstructionsHomeDir(env)  → ~/.zcode
    function getAgentUserInstructionsFilePath(env)
        → join(getUserInstructionsHomeDir(env), ".zcode", "AGENTS.md")

`ZCODE_HOME` 可以改根目录。它还有 project 级的 AGENTS.md / CLAUDE.md 链，
那是项目自己的事，不碰。

`resources\\glm\\zcode.cjs`（15.6 MB）里也压着系统提示词，但那是 webpack 产物，
改它要过 `node --check` 语法校验，校验不过客户端直接崩 —— 收益不如风险大，不碰。
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import HOME, Hit, Target, has_any, manual_root

#: 真实环境变量名（从 asar 里读到的，不是 ZCODE_DIR）
ENV_HOME = "ZCODE_HOME"

#: ZCode home 的特征名。AGENTS.md 是主靶点，其余是客户端自己会写的。
#: 不列 logs / cache / projects —— 太通用，任何目录都可能命中。
ZCODE_MARKS = ("AGENTS.md", "CLAUDE.md", "config.json", "settings.json",
               "history.jsonl")

#: 安装目录。用户没启动过客户端时 ~/.zcode 还不存在，靠这个判断「装了没」
INSTALL_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ZCode",
    Path("C:/Program Files/ZCode"),
]


class ZCode(Target):
    name = "zcode"
    label = "ZCode"
    procs = ("zcode.exe", "z-code.exe")

    def __init__(self, root: Path | None = None):
        env = os.environ.get(ENV_HOME)
        self.home = Path(env) if env else (root or (HOME / ".zcode"))

    @property
    def agents(self) -> Path:
        return self.home / "AGENTS.md"

    @classmethod
    def looks_like(cls, p: Path) -> tuple[bool, str]:
        """ZCode 的 home 目录，默认 `~/.zcode`。

        名字对上直接放行（空目录也合法：没启动过时就是空的）；名字不同则
        要看到点 ZCode 自己的东西才认。
        """
        if p.name.lower() in (".zcode", ".agents"):
            return True, ""
        if not has_any(p, ZCODE_MARKS):
            return False, ("这个目录里没有 ZCode 的东西（AGENTS.md 等），"
                           "选用户目录下的 .zcode 文件夹")
        return True, ""

    def detect(self) -> bool:
        self.manual_used = False
        manual, why = manual_root("zcode", self.looks_like)
        if manual:
            self.home = manual           # 手动指定的目录当 home 用
            self.manual_note = ""
            self.root = manual
            self.manual_used = True
            return True
        if why:
            self.manual_note = why
        if self.home.is_dir():
            self.root = self.home
            return True
        # 装了但没启动过：安装目录在也算，首次启动会自己建 ~/.zcode
        for d in INSTALL_DIRS:
            try:
                if d and d.is_dir() and (d / "ZCode.exe").exists():
                    self.root = d
                    return True
            except OSError:
                continue
        return False

    def collect(self) -> list[Hit]:
        # 只有一个靶点。上一版还写 memory/*.md，但 asar 里找不到任何「读这个
        # 目录当指令」的证据 —— 往那儿塞人格是往别人的记忆里投毒，撤了。
        return [Hit(self.agents, "block", "用户级 AGENTS.md")]
