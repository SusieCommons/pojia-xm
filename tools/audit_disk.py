"""注入后磁盘核对：逐个注入点验真。

不看程序自己说"注入成功"，直接读文件，验六件事：
  1. 真群号在，占位群号不在
  2. 标识串是直写形态，不带反斜杠
  3. 模块名是「叙事模块」，旧的「成人向模块」归零
  4. 纯文本 / 策略段通道恰好一处封口行，且无旧 doc 标记残留
  5. product.json / 缓存 JSON 合法
  6. 触发平台审核的固定模板串不在文件里

跑法：python tools/audit_disk.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import brand  # noqa: E402
from core import get as get_target  # noqa: E402
from core.base import Hit  # noqa: E402

PLACEHOLDER = "100000000"


def real_group() -> str:
    """真群号从发布副本读。

    源码仓的 `core/brand.py` 是占位号（硬规则，真号不能进库），
    打包副本 `build/brand_local/` 才是注入后的真号。
    副本不存在时退回占位号，并跳过群号断言 —— 那种情况下只能验形状。
    """
    copy = ROOT / "build" / "brand_local" / "core" / "brand.py"
    if copy.is_file():
        m = re.search(r'DEFAULT_GROUP_NO\s*=\s*"(\d+)"', read(copy))
        if m:
            return m.group(1)
    return brand.DEFAULT_GROUP_NO

HOME = Path.home()
#: 纯文本 / 策略段通道写在正文之后的封口行。1.5.0 起不再写 begin/end。
#: 老文件里可能还有等号 / HTML 注释两种旧形态，两个都认，用于报「旧标记残留」。
SEAL_RE = re.compile(r"<!--\s*xm-doc:seal\s+\d+:[0-9a-fA-F]{8}\s*-->")
OLD_DOC_OPEN = "========== xm-doc:begin =========="
OLD_DOC_CLOSE = "========== xm-doc:end =========="
SYS_OPEN = "========== xm-sys:begin =========="
SYS_CLOSE = "========== xm-sys:end =========="


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", "replace") if p.is_file() else ""


def main() -> int:
    hits: list[Hit] = []
    for name in ("codex", "wb", "zcode", "dsh"):
        try:
            t = get_target(name)
            if not t.detect():
                print(f"[SKIP] {name}: 未安装")
                continue
            hits.extend(t.collect())
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] {name}: {e}")
    if not hits:
        print("[FAIL] 一个注入点都没找到")
        return 2

    GROUP_ID = real_group()
    print(f"注入点 {len(hits)} 个  期望群号 {GROUP_ID}")
    bad = 0
    for h in hits:
        if not h.path.is_file():
            print(f"  缺失 {h.path}")
            bad += 1
            continue
        t = read(h.path)
        rel = f"{h.path.parent.name}/{h.path.name}"
        probs = []

        if str(GROUP_ID) in t:
            pass
        elif h.how in ("block", "policy", "sys", "text", "json", "apppol"):
            probs.append("无群号")

        if "100000000" in t:
            probs.append("有占位群号")
        if "AY-ONLINE\\|" in t:
            probs.append("标识串带反斜杠")
        if "成人向模块" in t:
            probs.append("旧模块名残留")
        if "系统检测到您当前输入的信息存在敏感内容" in t:
            probs.append("含平台拦截模板")
        # 封口行：每个注入点应当恰好一处（策略段 / AGENTS.md）。
        # json / sys 通道不走封口行 —— json 走 template 字段、sys 走 xm-sys 标记。
        if h.how in ("block", "policy", "apppol"):
            n = len(SEAL_RE.findall(t))
            if n == 0:
                probs.append("缺封口行")
            elif n > 1:
                probs.append(f"封口行 {n} 处（应 1）")
        if OLD_DOC_OPEN in t or OLD_DOC_CLOSE in t:
            probs.append("旧 doc 标记残留")
        if t.count(SYS_OPEN) != t.count(SYS_CLOSE):
            probs.append(f"sys标记不成对 {t.count(SYS_OPEN)}/{t.count(SYS_CLOSE)}")
        if h.path.suffix == ".json":
            try:
                json.loads(t)
            except ValueError as e:
                probs.append(f"JSON 非法 {e}")

        flag = "OK  " if not probs else "BAD "
        bad += bool(probs)
        print(f"  {flag} {rel:52} {len(t):>7}B {'; '.join(probs)}")

    print(f"\n问题文件 {bad}/{len(hits)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
