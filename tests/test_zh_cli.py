# -*- coding: utf-8 -*-
"""命令行帮助必须是中文 —— 界面上不能突然冒出英文骨架。

这个工具从文档到界面全是中文，只有 argparse 的骨架文案是英文的
（usage: / positional arguments: / options: / show this help message and exit）。
`core/zh_argparse.py` 接管了 argparse 的 `_()`，这里把行为锁住，
防止哪天升级 Python 或改入口时又漏回英文。
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.zh_argparse import _ZH, _translate, apply_zh  # noqa: E402


# 这些串是 argparse 源码里真实存在的字面量。少一条就是漏翻。
# 带格式占位符的必须逐字符一致，少一个 \n、少一个 %(x)s 都匹配不上。
MUST_HAVE = [
    "usage: ",
    "positional arguments",
    "options",
    "subcommands",
    "%(heading)s:",
    "show this help message and exit",
    "%(prog)s: error: %(message)s\n",
    "unrecognized arguments: %s",
    "the following arguments are required: %s",
    "invalid choice: %(value)r (choose from %(choices)s)",
    "argument %(argument_name)s: %(message)s",
]


def test_table_covers_required_keys():
    missing = [k for k in MUST_HAVE if k not in _ZH]
    assert not missing, f"翻译表漏了：{missing}"


def test_no_value_is_still_english():
    """译文里不该整条照抄原文 —— 那是漏翻而是能过表的假翻译。"""
    for k, v in _ZH.items():
        assert k != v, f"这条等于没翻：{k!r}"


def test_unknown_text_passthrough():
    """表里没有的串必须原样返回，不然会有未翻译的段落变成空白。"""
    assert _translate("some third-party option help") == "some third-party option help"


def test_apply_zh_is_idempotent():
    apply_zh()
    import argparse
    first = argparse._
    apply_zh()
    assert argparse._ is first, "重复调用换了函数对象，会让热重载场景出现两套"


def _help_text(module_name: str) -> str:
    """跑一次 --help，把 stdout 抓下来。"""
    mod = __import__(module_name)
    buf = io.StringIO()
    rc = None
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                mod.main(["--help"])
            except SystemExit as e:      # argparse 的 --help 走 SystemExit(0)
                rc = e.code
    except Exception as e:                # 入口不该炸，但别让测试因为炸了就当成通过
        raise AssertionError(f"{module_name} --help 抛异常：{e!r}") from e
    assert rc == 0, f"{module_name} --help 退出码 {rc}"
    return buf.getvalue()


def test_app_help_is_chinese():
    out = _help_text("app")
    for zh in ("用法：", "位置参数：", "选项：", "显示此帮助并退出"):
        assert zh in out, f"app.py --help 里没有 {zh!r}\n{out}"
    for en in ("usage:", "positional arguments", "options:", "show this help message"):
        assert en not in out, f"app.py --help 里还有英文 {en!r}\n{out}"


def test_xm_help_is_chinese():
    out = _help_text("xm")
    for zh in ("用法：", "位置参数：", "选项：", "显示此帮助并退出"):
        assert zh in out, f"xm.py --help 里没有 {zh!r}\n{out}"
    for en in ("usage:", "positional arguments", "options:", "show this help message"):
        assert en not in out, f"xm.py --help 里还有英文 {en!r}\n{out}"


def test_error_prefix_is_chinese():
    """错误路径比帮助更容易漏 —— 走的是另一条 format 串。"""
    mod = __import__("xm")
    buf = io.StringIO()
    rc = None
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            mod.main(["--definitely-not-a-flag"])
        except SystemExit as e:
            rc = e.code
    assert rc == 2, f"未知参数应以 2 退出，实际 {rc}"
    out = buf.getvalue()
    assert "错误：" in out, f"错误前缀还是英文\n{out}"
    assert "无法识别的参数" in out, f"错误正文还是英文\n{out}"
    assert "error:" not in out, f"还残留 error:\n{out}"
