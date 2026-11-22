# -*- coding: utf-8 -*-
"""门禁自己也要有测试 —— 不然它拦不拦得住没人知道。"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_clean",
                                              ROOT / "tools" / "check_clean.py")
cc = importlib.util.module_from_spec(spec)
sys.modules["check_clean"] = cc
spec.loader.exec_module(cc)


def test_detects_private_key():
    pats = [p for p, _ in cc.FORBIDDEN_TEXT]
    import re
    hit = any(re.search(p, "-----BEGIN RSA PRIVATE KEY-----") for p in pats)
    assert hit, "私钥没被拦"


def test_detects_public_ip():
    import re
    pats = [p for p, n in cc.FORBIDDEN_TEXT if n == "硬编码公网 IP"]
    assert any(re.search(p, "http://203.0.113.10/api") for p in pats)


def test_allows_localhost():
    import re
    pats = [p for p, n in cc.FORBIDDEN_TEXT if n == "硬编码公网 IP"]
    for url in ("http://127.0.0.1:8080/api", "http://localhost:8090"):
        assert not any(re.search(p, url) for p in pats), url


def test_detects_qr_image():
    import re
    pats = [p for p, n in cc.FORBIDDEN_TEXT if "二维码" in n]
    assert any(re.search(p, '<img src="assets/qq-group.png">') for p in pats)
    assert any(re.search(p, "![群](assets/qr-code.png)") for p in pats)


def test_allows_mentioning_keyword():
    """文档里说「不要放二维码」不该被拦。"""
    import re
    pats = [p for p, n in cc.FORBIDDEN_TEXT if "二维码" in n]
    text = "仓库里不放二维码，不放群图片。"
    assert not any(re.search(p, text) for p in pats)


def test_known_ids_blocked():
    assert "GROUP_ID_A" in cc.KNOWN_IDS
    assert "GROUP_ID_B" in cc.KNOWN_IDS


def test_worktree_is_clean():
    """这个仓库自己必须过门禁。"""
    bad = cc.check_worktree()
    assert bad == [], "\n".join(bad)
