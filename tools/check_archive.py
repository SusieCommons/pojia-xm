"""核对 PyInstaller 归档：依赖是否真打进包里。

pywebview 缺依赖时 PyInstaller 不报错 —— 包打得出来、命令行全对、双击没窗口。
所以每次打完必须数这几个条目，不能只看"构建成功"。

onefile 包不是标准 zip（PKG 追加在 PE 末尾），要用 PyInstaller 自己的
CArchiveReader 读；用 zipfile 会报 BadZipFile。必须用装了 pyinstaller 的解释器跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

p = Path(sys.argv[1] if len(sys.argv) > 1 else r"build\brand_local\dist151\xiaoxiongmao.exe")
arc = CArchiveReader(str(p))
names = list(arc.toc.keys())
print("exe", p)
print("条目", len(names))
for k in ("webview", "pythonnet", "clr_loader"):
    n = sum(1 for x in names if k in x.lower())
    print(f"{k:12} {n}")
print("persona.md ", any("persona.md" in x for x in names))
print("ui/index   ", any("index.html" in x for x in names))