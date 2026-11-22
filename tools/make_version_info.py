# -*- coding: utf-8 -*-
"""从 app.py 的 VERSION 生成 build/version_info.txt。

为什么需要这个：`build/` 在 .gitignore 里，CI checkout 下来是空的 ——
没有版本资源文件，PyInstaller 打出来的 exe 就没有 VS_VERSIONINFO，
`tools/verify_release.py` 的静态检查会判「exe 不带版本资源」。

版本号只有一个真源：`app.py:VERSION`。这个脚本从那里读，把四处
（filevers / prodvers / FileVersion / ProductVersion）一次写完，
避免手改漏掉某一处。

`filevers` 是四元组，只取前三段，第四段固定 0。`1.3.7` → `(1, 3, 7, 0)`。
带预发布后缀（`1.3.7-rc1`）时同样取前三段 —— Windows 的版本资源不接受
字符串后缀，那是 `FileVersion` 字段的事。

用法：
    python tools/make_version_info.py            # 写入 build/version_info.txt
    python tools/make_version_info.py --check    # 只检查，不一致返回 1
    python tools/make_version_info.py --print    # 打到 stdout，不写文件
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build", "version_info.txt")

TEMPLATE = """# UTF-8
#
# 由 tools/make_version_info.py 从 app.py:VERSION 生成，不要手改。
# 要改版本号：改 app.py 的 VERSION，再重跑这个脚本。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, 0),
    prodvers=({a}, {b}, {c}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404b0',
          [StringStruct('CompanyName', '小熊猫'),
           StringStruct('FileDescription', '小熊猫破甲'),
           StringStruct('FileVersion', '{ver}'),
           StringStruct('InternalName', 'xiaoxiongmao'),
           StringStruct('LegalCopyright', ''),
           StringStruct('OriginalFilename', '小熊猫破甲.exe'),
           StringStruct('ProductName', '小熊猫破甲'),
           StringStruct('ProductVersion', '{ver}')])
      ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def project_version() -> str:
    src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise SystemExit("app.py 里找不到 VERSION = \"x.y.z\"")
    return m.group(1)


def parts(ver: str) -> tuple[int, int, int]:
    """取版本号前三段数字。后缀（-rc1 / .dev0）直接丢掉。"""
    nums = re.findall(r"\d+", ver.split("-")[0].split("+")[0])
    if len(nums) < 3:
        raise SystemExit("版本号不足三段: %r" % ver)
    return int(nums[0]), int(nums[1]), int(nums[2])


def render(ver: str) -> str:
    a, b, c = parts(ver)
    return TEMPLATE.format(a=a, b=b, c=c, ver=ver)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 build/version_info.txt")
    ap.add_argument("--check", action="store_true", help="只检查是否一致")
    ap.add_argument("--print", dest="to_stdout", action="store_true", help="打到 stdout")
    a = ap.parse_args()

    ver = project_version()
    text = render(ver)

    if a.to_stdout:
        sys.stdout.write(text)
        return 0

    if a.check:
        if not os.path.exists(OUT):
            print("[FAIL] 不存在: %s" % OUT)
            return 1
        cur = io.open(OUT, encoding="utf-8").read()
        if cur != text:
            print("[FAIL] %s 与 app.py:VERSION=%s 不一致" % (OUT, ver))
            return 1
        print("[OK] %s 与 %s 一致" % (OUT, ver))
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # 原子写：先落临时文件再替换。打包脚本可能和这个脚本并行跑，
    # 读到半个文件会让 PyInstaller 报一个完全无关的语法错误。
    tmp = OUT + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print("已写入 %s（版本 %s）" % (OUT, ver))
    return 0


if __name__ == "__main__":
    sys.exit(main())
