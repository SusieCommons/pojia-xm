"""把 argparse 的内置文案换成中文。

argparse 把所有可翻译字符串都过一遍模块级的 `_()`（默认是 gettext 的
gettext()，没有 .mo 时原样返回英文）。这里换成查表：命中返回中文，
没命中原样返回 —— 不认识的新串继续显示英文，不会变成 key 或空串。

用法：

    from core.zh_argparse import apply_zh
    apply_zh()                                   # 改模块级 _()，全局生效
    ap = argparse.ArgumentParser(...)            # 必须在 apply_zh 之后构造

**必须在 ArgumentParser 构造之前调用。** `add_help=True` 在构造时会读取
`show this help message and exit` 的译文并固化到 -h 选项上，晚了就改不动。

覆盖的是 Python 3.10 ~ 3.14 里 argparse 用到的全部 _() 字面量。多翻几条
没坏处（用不上而已）；少翻一条就会在某个错误路径上冒出英文。
"""
from __future__ import annotations

import argparse

# key 是 argparse 源码里的英文字面量（含格式占位符，必须逐字符一致）
_ZH: dict[str, str] = {
    # ---- 结构 ----
    "usage: ": "用法： ",
    "positional arguments": "位置参数",
    "options": "选项",
    "subcommands": "子命令",
    "%(heading)s:": "%(heading)s：",
    # ---- 帮助 ----
    "show this help message and exit": "显示此帮助并退出",
    "show program's version number and exit": "显示版本号并退出",
    " (default: %(default)s)": "（默认 %(default)s）",
    # ---- 错误 / 警告前缀 ----
    # 注意这两个串末尾自带 \n，key 少写一个 \n 就匹配不上
    "%(prog)s: error: %(message)s\n": "%(prog)s：错误：%(message)s\n",
    "%(prog)s: warning: %(message)s\n": "%(prog)s：警告：%(message)s\n",
    "argument %(argument_name)s: %(message)s": "参数 %(argument_name)s：%(message)s",
    # ---- 参数校验 ----
    "unrecognized arguments: %s": "无法识别的参数：%s",
    "the following arguments are required: %s": "缺少必需参数：%s",
    "invalid choice: %(value)r (choose from %(choices)s)":
        "无效取值：%(value)r（可选 %(choices)s）",
    "invalid %(type)s value: %(value)r": "%(type)s 类型的值无效：%(value)r",
    "expected one argument": "需要一个参数",
    "expected at least one argument": "至少需要一个参数",
    "expected at most one argument": "最多只能有一个参数",
    "not allowed with argument %s": "不能与参数 %s 同时使用",
    "one of the arguments %s is required": "必须提供其中一个参数：%s",
    "ambiguous option: %(option)s could match %(matches)s":
        "选项有歧义：%(option)s 可能匹配 %(matches)s",
    "ignored explicit argument %r": "忽略了多余参数 %r",
    "unexpected option string: %s": "意外的选项字符串：%s",
    "unknown parser %(parser_name)r (choices: %(choices)s)":
        "未知的子命令 %(parser_name)r（可选：%(choices)s）",
    # ---- 以下本项目正常路径用不到，留着防止某天冒英文 ----
    "can't open '%(filename)s': %(error)s": "打不开 '%(filename)s'：%(error)s",
    'argument "-" with mode %r': '模式 %r 下不能用 "-" 作参数',
    "option '%(option)s' is deprecated": "选项 '%(option)s' 已弃用",
    "argument '%(argument_name)s' is deprecated": "参数 '%(argument_name)s' 已弃用",
    "command '%(parser_name)s' is deprecated": "命令 '%(parser_name)s' 已弃用",
}


def _translate(text: str) -> str:
    """argparse `_()` 的替代实现。只翻表里有的，其余原样返回。"""
    return _ZH.get(text, text)


def apply_zh() -> None:
    """接管 argparse 的 `_()`。重复调用无副作用。"""
    argparse._ = _translate       # type: ignore[attr-defined]
