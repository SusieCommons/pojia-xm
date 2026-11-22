# -*- coding: utf-8 -*-
"""标记段：写入、识别、剥离、残块处理。

revert 的可靠性完全建立在标记段上。这一组测试是它唯一的地基。
"""
from __future__ import annotations

from core import base


BODY = "你是测试角色。\n第二行内容。"
HEAD = "# 用户原有内容\n\n保留我。\n"


def test_put_block_roundtrip():
    out = base.put_block(HEAD, BODY)
    assert base.BEGIN in out
    assert base.END in out
    assert BODY in out
    assert HEAD.strip() in out


def test_has_block_after_put():
    assert base.has_block(base.put_block("", BODY)) is True


def test_has_block_false_on_plain():
    assert base.has_block("就是一段普通文字\n") is False


def test_has_block_true_on_orphan_head():
    """只有头没有尾也算有注入痕迹。

    历史 bug：要求头尾都在，于是有头无尾的残块永远清不掉 ——
    revert 报成功，客户端里那段人格还在。
    """
    assert base.has_block("前言\n" + base.BEGIN + "\n孤儿内容\n") is True


def test_strip_removes_block_keeps_original():
    text = base.put_block(HEAD, BODY)
    out = base.strip_block(text)
    assert base.BEGIN not in out
    assert base.END not in out
    assert BODY not in out
    assert "保留我。" in out


def test_strip_orphan_head_cuts_to_end():
    """有头无尾：从标记处切到文件末尾。"""
    text = "前面\n" + base.BEGIN + "\n残留内容\n后面也没了\n"
    out = base.strip_block(text)
    assert base.BEGIN not in out
    assert "残留内容" not in out
    assert "前面" in out


def test_strip_idempotent():
    """剥两次和剥一次结果一样。"""
    text = base.put_block(HEAD, BODY)
    once = base.strip_block(text)
    twice = base.strip_block(once)
    assert once == twice


def test_put_block_twice_no_duplicate():
    """重复注入不能叠加出两个块。"""
    text = base.put_block(HEAD, BODY)
    text = base.put_block(text, BODY)
    assert text.count(base.BEGIN) == 1
    assert text.count(base.END) == 1


def test_strip_empty_file():
    assert base.strip_block("") == ""


def test_put_policy_into_template():
    """WorkBuddy 的 prompt.tpl：注入点必须在 <content_policy> 段内。"""
    tpl = "HEAD\n<content_policy>\n原始策略\n</content_policy>\nTAIL\n"
    out = base.put_policy(tpl, BODY)
    assert base.BEGIN in out
    # 块必须在开闭标签之间，不能跑到 TAIL 后面
    i = out.index("<content_policy>")
    j = out.index("</content_policy>")
    k = out.index(base.BEGIN)
    assert i < k < j
    assert "TAIL" in out


def test_strip_policy_restores_template():
    tpl = "HEAD\n<content_policy>\n原始策略\n</content_policy>\nTAIL\n"
    out = base.strip_block(base.put_policy(tpl, BODY))
    assert base.BEGIN not in out
    assert "<content_policy>" in out
    assert "TAIL" in out


def test_residue_detects_legacy_marks():
    """老工具留下的特征串必须能被查出来。"""
    assert base.residue("正文里有 legacy-tool-a 这个词") != []
    assert base.residue("干净内容，没有任何特征串。") == []


def test_fingerprint_picks_distinctive_line():
    fp = base.fingerprint("# 标题\n\n你是测试角色，这一行足够长以通过指纹要求。\n")
    assert fp
    assert len(fp) >= 4
