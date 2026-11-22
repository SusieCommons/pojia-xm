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


# 裸 IP（不在 URL 里）是后补的规则。起因是白名单配置里的 `allow <IP>;`
# 里留了一个真实的出口 IP `allow 203.0.113.10;`，而当时的门禁只认
# URL 里的 IP，这条一路绿灯过了。
BARE = "硬编码公网 IP（裸）"


def _bare_pat():
    return [p for p, n in cc.FORBIDDEN_TEXT if n == BARE]


def test_detects_bare_ip():
    import re
    pat = _bare_pat()
    assert pat, "裸 IP 规则不见了"
    for s in ("allow 203.0.113.10;", "bind 8.8.8.8", "host 93.184.216.34"):
        assert any(re.search(p, s) for p in pat), s


def test_bare_ip_allows_private_and_reserved():
    """私网、回环、文档保留段都不该拦 —— 它们在示例配置里必然出现。"""
    import re
    pat = _bare_pat()
    for s in ("allow 127.0.0.1;", "allow 10.1.2.3;", "allow 192.168.1.1;",
              "allow 172.16.5.4;", "allow 169.254.1.1;", "0.0.0.0",
              "allow 203.0.113.10;", "allow 198.51.100.7;"):
        assert not any(re.search(p, s) for p in pat), s


def test_bare_ip_allows_non_ip_numbers():
    """段值超过 255 的四段数字不是 IP。

    注意 `1.5.2.9` 这类**不能**放进这个测试 —— 它是合法公网地址，
    规则拦它是对的。这里只用真正越界的数值。
    """
    import re
    pat = _bare_pat()
    for s in ("transform: translate(2.774.868.86)", "1.999.888.777",
              "margin: 300.400.500.600"):
        assert not any(re.search(p, s) for p in pat), s


def test_bare_ip_172_edge():
    """172.16/12 是私网，172.32 不是 —— 边界不能写宽。"""
    import re
    pat = _bare_pat()
    assert not any(re.search(p, "172.31.255.254") for p in pat)
    assert any(re.search(p, "172.32.0.1") for p in pat)


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
