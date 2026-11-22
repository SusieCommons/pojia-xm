# -*- coding: utf-8 -*-
"""编码：注入写 UTF-8，还原还原始字节。

这两条是分开的两件事，缺一条就会出「提示成功但没生效」或者
「还原了但文件永久变成 UTF-8」。
"""
from __future__ import annotations

import pytest

from core import base


PERSONA = "# 测试\n\n你是测试角色，这一行足够长以通过指纹要求。\n"


# ---------------------------------------------------------------- 解码

@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "gbk", "utf-16"])
def test_decode_roundtrip(enc):
    """六种常见编码都要能正确解码回原文。"""
    origin = "# 我的人格\n\n你是助手。\n"
    raw = origin.encode(enc)
    assert base.decode_bytes(raw) == origin


def test_decode_utf16_nobom():
    """无 BOM 的 UTF-16LE 是最阴的一种。

    按 UTF-8 解码这种字节**不会报错**，只会得到带 NUL 的垃圾串。
    如果 decode_bytes 退回 utf-8 成功，注入时就会把垃圾写回去，原文毁掉。
    """
    origin = "# 我的人格\n\n你是助手。\n"
    raw = origin.encode("utf-16-le")
    got = base.decode_bytes(raw)
    assert got == origin, "解出来是 %r" % got[:40]


def test_decode_utf16be_nobom():
    origin = "你是助手。ABC\n"
    raw = origin.encode("utf-16-be")
    assert base.decode_bytes(raw) == origin


def test_looks_utf16_rejects_plain_ascii():
    """普通 ASCII 不该被判成 UTF-16。"""
    assert base._looks_utf16(b"hello world this is plain ascii\n") is False


def test_looks_utf16_rejects_odd_length():
    assert base._looks_utf16(b"abc") is False


def test_decode_fallback_never_raises():
    """任意字节都不能让解码抛异常 —— 抛了调用方就没法继续。"""
    for raw in (b"\xff\xfe\x00", b"\x80\x81\x82", b"", b"\x00" * 9):
        assert isinstance(base.decode_bytes(raw), str)


# ---------------------------------------------------------------- 写入

def test_write_like_is_utf8(sandbox, tmp_path):
    """注入必须写 UTF-8，客户端才读得到。"""
    p = tmp_path / "AGENTS.md"
    p.write_bytes("旧内容 GBK".encode("gbk"))

    base.write_like(p, PERSONA)
    raw = p.read_bytes()
    assert raw.decode("utf-8") == PERSONA


def test_write_orig_preserves_bytes(sandbox, tmp_path):
    """还原必须逐字节还回原编码。"""
    p = tmp_path / "AGENTS.md"
    origin = "我的人格，中文内容。".encode("gbk")
    p.write_bytes(origin)

    base.write_orig(p, origin)
    assert p.read_bytes() == origin


def test_write_orig_utf16_bom(sandbox, tmp_path):
    p = tmp_path / "AGENTS.md"
    origin = "我的人格".encode("utf-16")
    base.write_orig(p, origin)
    assert p.read_bytes() == origin
    assert p.read_bytes().startswith(b"\xff\xfe")


def test_atomic_write_no_partial(sandbox, tmp_path):
    """原子写：写完之后目录里不该留下临时文件。"""
    p = tmp_path / "x.md"
    base.atomic_write(p, "内容" * 100)
    leftovers = [x.name for x in tmp_path.iterdir() if x.name != "x.md"]
    assert leftovers == []


def test_clear_readonly(sandbox, tmp_path):
    """只读文件要能被清掉属性后写入，不能直接失败。"""
    p = tmp_path / "ro.md"
    p.write_text("原内容", encoding="utf-8")
    import stat
    p.chmod(stat.S_IREAD)
    try:
        base._clear_readonly(p)
        base.write_like(p, PERSONA)
        assert p.read_text(encoding="utf-8") == PERSONA
    finally:
        p.chmod(stat.S_IWRITE | stat.S_IREAD)
