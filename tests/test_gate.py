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


# 裸 IP（不在 URL 里）是后补的规则。起因是白名单配置里留了一个真实的
# 出口 IP，而当时的门禁只认 URL 里的地址，那条一路绿灯过了。
# 测试里用文档保留段（203.0.113.x）当样本 —— 它本身就是「示例专用」，
# 放真地址进来等于又泄一次。
BARE = "硬编码公网 IP（裸）"


def _bare_pat():
    return [p for p, n in cc.FORBIDDEN_TEXT if n == BARE]


def test_detects_bare_ip():
    import re
    pat = _bare_pat()
    assert pat, "裸 IP 规则不见了"
    # 样本用公网 DNS / benchmark 段 / 一个普通公网段，不用真实业务地址 ——
    # 测试文件也是要公开的，写进去等于换个地方再泄一次。
    for s in ("allow 198.18.7.42;", "bind 8.8.8.8", "host 93.184.216.34"):
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
    """指纹表非空，且比对逻辑真的能命中。

    这里**不写真实号码** —— 测试文件也是公开的，把号码放进断言等于
    换个地方再泄一次。改用构造样本：随便取一个串，把它的指纹临时塞进
    表里，验证 `_find_known_ids` 能认出来。
    """
    sample = "998877665544"          # 虚构号码，不存在于任何真实账号
    expect = cc._id_sha16(sample)
    cc.KNOWN_ID_HASHES.add(expect)
    try:
        # 三种写法都要命中：裸串、被引号包住、在注释里
        for line in (sample,
                     'group_no = "%s"' % sample,
                     "# 联系 %s 进群" % sample):
            hits = cc._find_known_ids(line)
            assert hits, "没拦住：%r" % line
            assert all(h == expect for _, h in hits)
            # 只返回指纹，绝不返回明文
            assert sample not in repr(hits)
    finally:
        cc.KNOWN_ID_HASHES.discard(expect)


def test_known_ids_not_leaked_in_source():
    """门禁源码里不得出现任何真实号码的明文。

    这是回归测试：上一版把群号直接写在 KNOWN_IDS 里，同时又把自己放进
    SELF_EXEMPT，于是「泄露者不检查自己」，门禁一路绿灯推了上去。
    """
    src = io.open(ROOT / "tools" / "check_clean.py", encoding="utf-8").read()
    for h in cc.KNOWN_ID_HASHES:
        assert h in src, "指纹 %s 不在源码里？" % h
    # 源码里所有 8-24 位连续串，其指纹都不该命中已知表
    import re
    for m in re.finditer(r"(?<![0-9A-Za-z])([0-9A-Za-z]{8,24})(?![0-9A-Za-z])", src):
        cand = m.group(1)
        assert cc._id_sha16(cand) not in cc.KNOWN_ID_HASHES, \
            "源码里出现了已知号码的明文：%r" % cand


def test_worktree_is_clean():
    """这个仓库自己必须过门禁。"""
    bad = cc.check_worktree()
    assert bad == [], "\n".join(bad)
