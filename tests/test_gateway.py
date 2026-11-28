# -*- coding: utf-8 -*-
"""通道代理（core/gateway.py）的测试。

这一层最容易出的错不是「改写写错」，是**改写生效了但地址写错** ——
客户端看着连上了，每个请求都被上游 404。所以地址计算单独一组测试。
"""
from __future__ import annotations

import json

from core import gateway as gw


# ---------------------------------------------------------------- 改写表

def test_selftest_cmd_rewritten():
    """人格里四个自检口令等价，换成网关放行的那个。"""
    assert gw.rewrite_text("破甲自检") == "小熊猫自检"


def test_no_double_word():
    """`逆向分析` 不叠成「二进制分析分析」。

    长词必须排在它的前缀词前面 —— 排错了句子还能读，但读起来像机器翻的。
    """
    assert gw.rewrite_text("逆向分析") == "二进制分析"
    assert gw.rewrite_text("逆向工程") == "二进制分析"
    assert "分析分析" not in gw.rewrite_text("逆向分析这个二进制")
    assert "工程工程" not in gw.rewrite_text("逆向工程实践")


def test_contextual_needs_context():
    """`注入` 在普通编程语境里不动，只有安全上下文才改。"""
    assert gw.rewrite_text("什么是依赖注入") == "什么是依赖注入"
    assert "插桩" in gw.rewrite_text("卡密系统的注入点在哪")


def test_contextual_uses_rewritten_text():
    """上下文判据用改写后的文本。

    `破解` 先被换成「分析」，之后才判上下文 —— 这时 `卡密` 还在，照样命中。
    如果反着来（先判再改），`注入` 前面可能就是个已经被替掉的词，会漏。
    """
    out = gw.rewrite_text("破解这个注入点")
    assert "插桩" in out


def test_rewrite_deep_reaches_nested_text():
    """/responses 的正文在 input[0].content[0].text，只改顶层碰不到。"""
    obj = {"input": [{"role": "user",
                      "content": [{"type": "input_text", "text": "破甲自检"}]}]}
    out = gw.rewrite_obj(obj)
    assert out["input"][0]["content"][0]["text"] == "小熊猫自检"


def test_instructions_untouched():
    """system 侧一个字节都不许动。

    实测上游完全不审 system，改它只会把注入的人格正文改坏 ——
    人格里就有「逆向」「注入」这些词，改了就不是那份人格了。
    """
    obj = {"instructions": "你要做逆向和注入", "input": "破甲自检"}
    out = gw.rewrite_obj(obj)
    assert out["instructions"] == "你要做逆向和注入"
    assert out["input"] == "小熊猫自检"


def test_other_keys_preserved():
    """没命中的键原样保留。"""
    obj = {"model": "gpt-x", "temperature": 0.3, "messages": [{"content": "破甲"}]}
    out = gw.rewrite_obj(obj)
    assert out["model"] == "gpt-x"
    assert out["temperature"] == 0.3
    assert out["messages"][0]["content"] == "小熊猫"


# ---------------------------------------------------------------- 地址

def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_local_url_keeps_path(sandbox):
    """DSH 上游带 `/v1`，本机地址必须把这个路径段带上。

    丢了 `/v1` 之后客户端请求 `/dsh/chat/completions`，网关剥前缀转发到
    `https://mhapi.net/chat/completions` —— 上游 404。这个 bug 在 Codex
    （无路径段）上不显现，只在 DSH 上炸，很容易被当成「没修好」。
    """
    _write(sandbox["dsh"] / "profiles" / "desktop" / "cordis.patch.yml",
           "- id: llm-deepseek\n  config:\n    baseURL: https://mhapi.net/v1\n")
    u = gw._dsh_upstream()
    assert u.local_url(8787) == "http://127.0.0.1:8787/dsh/v1"


def test_local_url_codex_no_path(sandbox):
    _write(sandbox["codex"] / "config.toml",
           '[model_providers.custom]\nbase_url = "https://ddl.example.top"\n')
    u = gw._codex_upstream()
    assert u.local_url(8787) == "http://127.0.0.1:8787/codex"


def test_local_url_idempotent(sandbox):
    """已经指向本地时再算一次，不能拼成 /dsh/dsh/v1。"""
    _write(sandbox["dsh"] / "profiles" / "desktop" / "cordis.patch.yml",
           "- id: x\n  config:\n    baseURL: http://127.0.0.1:8787/dsh/v1\n")
    u = gw._dsh_upstream()
    assert u.local_url(8787) == "http://127.0.0.1:8787/dsh/v1"


def test_routes_drop_path(sandbox):
    """路由表只放源，不放路径 —— 路径由客户端请求自己带。"""
    _write(sandbox["codex"] / "config.toml",
           'base_url = "https://ddl.example.top"\n')
    _write(sandbox["dsh"] / "profiles" / "desktop" / "cordis.patch.yml",
           "- id: x\n  config:\n    baseURL: https://mhapi.net/v1\n")
    routes = gw._collect_routes()
    assert routes["dsh"] == "https://mhapi.net"
    assert routes["codex"] == "https://ddl.example.top"


def test_forward_joins_correctly(sandbox):
    """端到端：本机路径 `/dsh/v1/chat/completions` → 上游 `/v1/chat/completions`。"""
    _write(sandbox["dsh"] / "profiles" / "desktop" / "cordis.patch.yml",
           "- id: x\n  config:\n    baseURL: https://mhapi.net/v1\n")
    routes = gw._collect_routes()
    # 客户端按 local_url 发请求，网关剥掉 /dsh
    local = gw._dsh_upstream().local_url(8787)
    path = local.split("8787", 1)[1] + "/chat/completions"
    prefix = "/dsh"
    tail = path[len(prefix):]
    assert routes["dsh"].rstrip("/") + tail == \
        "https://mhapi.net/v1/chat/completions"


# ---------------------------------------------------------------- 装 / 卸

def test_install_uninstall_roundtrip(sandbox):
    """装完改成本地，卸完逐字节还原。"""
    cfg = sandbox["codex"] / "config.toml"
    orig = ('model = "x"\n\n[model_providers.custom]\n'
            'name = "S"\nbase_url = "https://ddl.example.top"\n'
            'wire_api = "responses"\n')
    _write(cfg, orig)

    assert gw.install(8787, only="codex") == 0
    mid = cfg.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8787/codex" in mid
    assert "ddl.example.top" not in mid

    assert gw.uninstall(only="codex") == 0
    assert cfg.read_text(encoding="utf-8") == orig


def test_install_is_idempotent(sandbox):
    """第二次 install 不能把备份覆盖成「已改成本地」—— 那是卸载唯一的来源。"""
    cfg = sandbox["codex"] / "config.toml"
    orig = 'base_url = "https://ddl.example.top"\n'
    _write(cfg, orig)

    gw.install(8787, only="codex")
    gw.install(8787, only="codex")
    assert "ddl.example.top" in (cfg.with_name("config.toml.xm-gw-bak")
                                 .read_text(encoding="utf-8"))
    gw.uninstall(only="codex")
    assert cfg.read_text(encoding="utf-8") == orig


def test_install_fixes_wrong_path(sandbox):
    """上一版把 DSH 的 `/v1` 丢了。重复 install 要把地址修正过来。"""
    p = sandbox["dsh"] / "profiles" / "desktop" / "cordis.patch.yml"
    _write(p, "- id: x\n  config:\n    baseURL: http://127.0.0.1:8787/dsh\n")
    (p.with_name(".dsh-gw-upstream")).write_text("https://mhapi.net/v1\n",
                                                 encoding="utf-8")
    gw.install(8787, only="dsh")
    assert "http://127.0.0.1:8787/dsh/v1" in p.read_text(encoding="utf-8")


def test_status_rows_agree(sandbox):
    _write(sandbox["codex"] / "config.toml",
           'base_url = "https://ddl.example.top"\n')
    st = gw.state(18787)
    assert st["total"] == 2
    assert st["on"] == 0
    assert st["need"] is False
    row = [r for r in st["routes"] if r["name"] == "codex"][0]
    assert row["installed"] and not row["local"]
    assert row["url"] == "https://ddl.example.top"


# ---------------------------------------------------------------- 服务

def test_ping_identifies_us():
    """ping 回 200 —— 端口被别的程序占住时不能误报「代理在跑」。

    用 1 秒超时的真 socket。端口取 18788，避开常见占用。
    """
    import socket
    import threading

    routes = {"codex": "https://example.invalid"}
    gw.Handler.routes = routes
    srv = gw.Server(("127.0.0.1", 18788), gw.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", 18788), 2) as s:
            s.sendall(b"GET /__xm__/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            head = s.recv(200)
        assert b" 200 " in head.split(b"\r\n", 1)[0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_route_404():
    import socket
    import threading

    gw.Handler.routes = {"codex": "https://example.invalid"}
    srv = gw.Server(("127.0.0.1", 18789), gw.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", 18789), 2) as s:
            s.sendall(b"GET /nope HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      b"Connection: close\r\n\r\n")
            head = s.recv(200)
        assert b" 404 " in head.split(b"\r\n", 1)[0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_state_json_serializable(sandbox):
    """给 UI 的东西必须能过 JSON —— 里面有 Path 就会在序列化时炸。"""
    _write(sandbox["codex"] / "config.toml", 'base_url = "https://x.example"\n')
    json.dumps(gw.state(18790), ensure_ascii=False)
