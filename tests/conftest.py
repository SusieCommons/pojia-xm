# -*- coding: utf-8 -*-
"""测试夹具：把四个客户端的环境变量全部指向临时目录。

任何测试都不许碰真实的 ~/.codex / ~/.workbuddy / ~/.zcode / ~/.dsh。
conftest 在收集阶段就把环境变量改掉，比每个测试自己改更可靠 ——
漏一个测试就会写进真实用户目录。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_ENV_KEYS = ("CODEX_HOME", "WB_HOME", "ZCODE_HOME", "DSH_HOME",
             "DSH_AGENTS_HOME", "XM_DATA", "HOME", "USERPROFILE")


@pytest.fixture()
def sandbox(monkeypatch):
    """一个隔离的临时根目录，四个客户端的 home 都在它下面。"""
    tmp = Path(tempfile.mkdtemp(prefix="xm-test-"))
    try:
        codex = tmp / "codex"
        wb = tmp / "workbuddy"
        zcode = tmp / "zcode"
        dsh = tmp / "dsh"
        data = tmp / "_data"
        for d in (codex, wb, zcode, dsh, data):
            d.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("CODEX_HOME", str(codex))
        monkeypatch.setenv("WB_HOME", str(wb))
        monkeypatch.setenv("ZCODE_HOME", str(zcode))
        monkeypatch.setenv("DSH_HOME", str(dsh))
        monkeypatch.setenv("DSH_AGENTS_HOME", str(tmp / "agents"))
        monkeypatch.setenv("XM_DATA", str(data))

        # base 模块在 import 时就把 DATA_DIR 算出来了，必须 reload
        import core.base as base
        importlib.reload(base)

        yield {
            "root": tmp,
            "codex": codex,
            "wb": wb,
            "zcode": zcode,
            "dsh": dsh,
            "data": data,
            "base": base,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def persona_text() -> str:
    """一段够长、能过指纹要求的正文。"""
    return (
        "# 测试人格\n\n"
        "你是一个用于验证注入链路的测试角色，这段正文不属于任何真实产品。\n"
        "第二行用来保证长度足够，指纹提取能找到一句有辨识度的内容。\n"
        "第三行收尾。\n"
    )
