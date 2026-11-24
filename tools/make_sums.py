"""生成仓库根目录的 SHA256SUMS.txt（覆盖全部跟踪文件）。

用法：
    python tools/make_sums.py          # 写 SHA256SUMS.txt
    python tools/make_sums.py --check  # 只校验，不写
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "SHA256SUMS.txt"


def tracked_files() -> list[str]:
    p = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8")
    files = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    # 清单本身不算进去，否则改一次就对不上了
    return [f for f in files if f != OUT.name]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text_normalized(path: Path) -> str:
    """按「仓库内容」算哈希：先把 CRLF 折成 LF 再算。

    为什么需要这个：仓库里有 .gitattributes 声明 text=auto eol=lf，
    但 `core.autocrlf=true` 的用户 clone 下来仍是 CRLF（那是工作区行为，
    .gitattributes 的 eol 只影响 checkout 后的规范化，不改用户配置）。
    拿工作区字节直接算哈希，同一份提交在不同机器上会得出不同结果 ——
    校验清单就失去意义了。

    统一按 LF 算，两边才对得上。
    """
    data = path.read_bytes()
    if b"\r\n" in data:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def build() -> str:
    lines = []
    for rel in sorted(tracked_files()):
        p = ROOT / rel
        if not p.exists():
            continue
        lines.append("%s  %s" % (sha256_text_normalized(p), rel.replace("\\", "/")))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="make_sums")
    ap.add_argument("--check", action="store_true", help="只校验不写")
    a = ap.parse_args(argv[1:])

    want = build()
    if a.check:
        if not OUT.exists():
            print("[X] 缺 SHA256SUMS.txt")
            return 2
        # 清单本身也可能被 CRLF 化（README 提过这条坑），比较前先折成 LF
        have = OUT.read_text(encoding="utf-8").replace("\r\n", "\n")
        if have != want:
            print("[X] SHA256SUMS.txt 与当前文件不一致，重跑一次生成")
            return 2
        print("[OK] 全部文件校验通过（%d 项，按 LF 归一）" % want.count("\n"))
        return 0

    OUT.write_text(want, encoding="utf-8", newline="\n")
    print("[OK] 写入 %d 项" % (want.count("\n")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))