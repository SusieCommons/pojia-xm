# -*- coding: utf-8 -*-
"""注入 → 校验 → 还原 全链路，四个客户端各跑一遍。

重点验的是「还原」，不是「注入」。注入写进去了用户马上能看见；
还原没干净，用户看不见，只会觉得工具骗人。

真实 API：
    t.detect() -> bool
    t.collect() -> list[Hit]
    t.inject(persona, dry=False) -> Report
    t.verify(persona="") -> Report
    t.revert() -> Report
    Report 有 .ok / .found / .changed / .skipped / .line()
"""
from __future__ import annotations

import importlib

import pytest

PERSONA = (
    "# 测试人格\n\n"
    "你是测试角色，这段正文用来验证注入与还原的完整链路。\n"
    "第二行保证长度足够，指纹提取能找到有辨识度的内容。\n"
)


def _targets(sandbox):
    """在当前 sandbox 环境下重新加载四个靶点模块。"""
    import core.codex as codex
    import core.wb as wb
    import core.zcode as zcode
    import core.dsh as dsh
    for m in (codex, wb, zcode, dsh):
        importlib.reload(m)
    return {
        "codex": codex.Codex(),
        "wb": wb.WorkBuddy(),
        "zcode": zcode.ZCode(),
        "dsh": dsh.DSH(),
    }


def _mk_codex(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")


def _mk_wb(home):
    for plug in ("code", "design", "work"):
        d = home / "plugins" / "cache" / "workbuddy-builtin" / f"welcomemode-{plug}" / "1.0.0"
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.tpl").write_text(
            "HEAD\n<content_policy>\n原始策略\n</content_policy>\nTAIL\n",
            encoding="utf-8")
    (home / "SOUL.md").write_text("原始 SOUL\n", encoding="utf-8")


def _mk_agents(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "AGENTS.md").write_text("# 我自己的指令\n\n保留这一行。\n", encoding="utf-8")


# ---------------------------------------------------------------- ZCode

def test_zcode_apply_verify_revert(sandbox):
    _mk_agents(sandbox["zcode"])
    t = _targets(sandbox)["zcode"]
    assert t.detect() is True

    hits = t.collect()
    assert hits, "没找到靶点"

    rep = t.inject(PERSONA)
    assert rep.ok, rep.line()

    text = (sandbox["zcode"] / "AGENTS.md").read_text(encoding="utf-8")
    assert "测试角色" in text
    assert "保留这一行" in text

    rep = t.verify(PERSONA)
    # verify() 返回 [{ok, text, hint}]，不是 Report
    assert all(x["ok"] for x in rep), [x for x in rep if not x["ok"]]

    rep = t.revert()
    assert rep.ok, rep.line()
    text = (sandbox["zcode"] / "AGENTS.md").read_text(encoding="utf-8")
    assert "测试角色" not in text
    assert "保留这一行" in text


def test_zcode_revert_after_double_apply(sandbox):
    """注入两次再还原 —— 这是历史 bug 的现场。

    旧实现用备份写回，第二次注入时备份里存的是注入态，还原等于原地覆盖。
    """
    _mk_agents(sandbox["zcode"])
    t = _targets(sandbox)["zcode"]
    t.inject(PERSONA)
    t.inject(PERSONA)
    t.revert()
    text = (sandbox["zcode"] / "AGENTS.md").read_text(encoding="utf-8")
    assert "测试角色" not in text
    assert "保留这一行" in text


def test_zcode_revert_twice(sandbox):
    """重复 revert 不报错、不破坏文件。"""
    _mk_agents(sandbox["zcode"])
    t = _targets(sandbox)["zcode"]
    t.inject(PERSONA)
    assert t.revert().ok
    assert t.revert().ok
    assert "保留这一行" in (sandbox["zcode"] / "AGENTS.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- DSH

def test_dsh_apply_revert(sandbox):
    _mk_agents(sandbox["dsh"])
    t = _targets(sandbox)["dsh"]
    assert t.detect() is True
    t.inject(PERSONA)
    assert "测试角色" in (sandbox["dsh"] / "AGENTS.md").read_text(encoding="utf-8")
    t.revert()
    assert "测试角色" not in (sandbox["dsh"] / "AGENTS.md").read_text(encoding="utf-8")


def test_dsh_does_not_create_shared_agents(sandbox):
    """~/.agents 不存在时不能去创建它 —— 那是别的工具在用的共享目录。"""
    _mk_agents(sandbox["dsh"])
    shared = sandbox["root"] / "agents"
    assert not shared.exists()

    t = _targets(sandbox)["dsh"]
    t.inject(PERSONA)
    assert not shared.exists(), "DSH 不该创建共享的 ~/.agents"


def test_dsh_writes_existing_shared_agents(sandbox):
    """但如果 ~/.agents/AGENTS.md 已经存在，就该一起处理。"""
    _mk_agents(sandbox["dsh"])
    shared = sandbox["root"] / "agents"
    shared.mkdir()
    (shared / "AGENTS.md").write_text("# 共享指令\n", encoding="utf-8")

    t = _targets(sandbox)["dsh"]
    t.inject(PERSONA)
    assert "测试角色" in (shared / "AGENTS.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- WorkBuddy

def test_wb_writes_all_versions(sandbox):
    """同一插件有多个版本目录时，全部都要写到。

    只写最新的那个，用户切回旧版本目录就看不到人格。
    """
    _mk_wb(sandbox["wb"])
    for ver in ("1.0.0", "1.0.1", "1.0.2"):
        d = sandbox["wb"] / "plugins" / "cache" / "workbuddy-builtin" / "welcomemode-code" / ver
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.tpl").write_text(
            "HEAD\n<content_policy>\n原始策略\n</content_policy>\nTAIL\n", encoding="utf-8")

    t = _targets(sandbox)["wb"]
    assert t.detect() is True
    t.inject(PERSONA)

    for ver in ("1.0.0", "1.0.1", "1.0.2"):
        f = (sandbox["wb"] / "plugins" / "cache" / "workbuddy-builtin"
             / "welcomemode-code" / ver / "prompt.tpl")
        assert "测试角色" in f.read_text(encoding="utf-8"), "版本 %s 没写到" % ver


def test_wb_skips_template_without_policy(sandbox):
    """没有 <content_policy> 的模板不能当靶点 —— 往里塞会破坏 Jinja。"""
    _mk_wb(sandbox["wb"])
    d = sandbox["wb"] / "plugins" / "cache" / "workbuddy-builtin" / "welcomemode-code" / "9.9.9"
    d.mkdir(parents=True, exist_ok=True)
    (d / "prompt.tpl").write_text("HEAD\n没有策略段\nTAIL\n", encoding="utf-8")

    t = _targets(sandbox)["wb"]
    t.inject(PERSONA)
    text = (d / "prompt.tpl").read_text(encoding="utf-8")
    assert "测试角色" not in text


def test_wb_revert_restores_template(sandbox):
    _mk_wb(sandbox["wb"])
    t = _targets(sandbox)["wb"]
    t.inject(PERSONA)
    t.revert()
    f = (sandbox["wb"] / "plugins" / "cache" / "workbuddy-builtin"
         / "welcomemode-code" / "1.0.0" / "prompt.tpl")
    text = f.read_text(encoding="utf-8")
    assert "测试角色" not in text
    assert "<content_policy>" in text
    assert "TAIL" in text


# ---------------------------------------------------------------- Codex

def test_codex_apply_sets_config_key(sandbox):
    _mk_codex(sandbox["codex"])
    t = _targets(sandbox)["codex"]
    assert t.detect() is True
    t.inject(PERSONA)

    cfg = (sandbox["codex"] / "config.toml").read_text(encoding="utf-8")
    assert "model_instructions_file" in cfg
    body = (sandbox["codex"] / "xm-persona.md")
    assert body.exists()
    assert "测试角色" in body.read_text(encoding="utf-8")


def test_codex_revert_removes_key(sandbox):
    """还原必须把配置键一起删掉，不能留悬空引用。"""
    _mk_codex(sandbox["codex"])
    t = _targets(sandbox)["codex"]
    t.inject(PERSONA)
    t.revert()

    cfg = (sandbox["codex"] / "config.toml").read_text(encoding="utf-8")
    assert "model_instructions_file" not in cfg
    assert 'model = "gpt-5"' in cfg


def test_codex_keeps_user_own_persona_file(sandbox):
    """用户自己写的 xm-persona.md 不能被删。"""
    _mk_codex(sandbox["codex"])
    mine = sandbox["codex"] / "xm-persona.md"
    mine.write_text("# 我自己写的\n\n这段内容属于用户。\n", encoding="utf-8")

    t = _targets(sandbox)["codex"]
    t.revert()
    assert mine.exists(), "用户的原文被删了"
    assert "这段内容属于用户" in mine.read_text(encoding="utf-8")


def test_codex_revert_leaves_clean_toml(sandbox):
    _mk_codex(sandbox["codex"])
    t = _targets(sandbox)["codex"]
    before = (sandbox["codex"] / "config.toml").read_text(encoding="utf-8")
    t.inject(PERSONA)
    t.revert()
    after = (sandbox["codex"] / "config.toml").read_text(encoding="utf-8")
    assert after.strip() == before.strip()


# ---------------------------------------------------------------- 编码 × 注入

@pytest.mark.parametrize("enc", ["gbk", "utf-16", "utf-16-le"])
def test_inject_then_revert_restores_encoding(sandbox, enc):
    """注入写 UTF-8，还原还回原编码 —— 两半都要对。"""
    sandbox["zcode"].mkdir(parents=True, exist_ok=True)
    f = sandbox["zcode"] / "AGENTS.md"
    origin = "# 我的指令\n\n这段是用户原文。\n"
    f.write_bytes(origin.encode(enc))

    t = _targets(sandbox)["zcode"]
    t.inject(PERSONA)

    # 注入后必须是 UTF-8，客户端才读得到
    injected = f.read_bytes()
    assert "测试角色" in injected.decode("utf-8")

    t.revert()
    # 还原后必须逐字节回到原编码
    assert f.read_bytes() == origin.encode(enc)


def test_inject_into_empty_file(sandbox):
    """0 字节文件能正常注入。"""
    sandbox["zcode"].mkdir(parents=True, exist_ok=True)
    f = sandbox["zcode"] / "AGENTS.md"
    f.write_bytes(b"")

    t = _targets(sandbox)["zcode"]
    rep = t.inject(PERSONA)
    assert rep.ok
    assert "测试角色" in f.read_text(encoding="utf-8")


def test_inject_into_cjk_path(sandbox, monkeypatch):
    """中文用户名路径。"""
    home = sandbox["root"] / "用户" / "张三" / "zcode"
    home.mkdir(parents=True, exist_ok=True)
    (home / "AGENTS.md").write_text("# 原文\n", encoding="utf-8")

    monkeypatch.setenv("ZCODE_HOME", str(home))
    import core.zcode as zcode
    importlib.reload(zcode)
    t = zcode.ZCode()
    t.inject(PERSONA)
    assert "测试角色" in (home / "AGENTS.md").read_text(encoding="utf-8")
    t.revert()
    assert "测试角色" not in (home / "AGENTS.md").read_text(encoding="utf-8")
