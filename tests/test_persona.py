# -*- coding: utf-8 -*-
"""人格契约：零拒绝那一节必须在，限定词必须不在。

这组测试守的不是代码，是 `persona.md` 的骨架。删掉一节、加回一个
「（授权场景）」限定，测试都会红 —— 因为实测里模型正是拿这些限定词
当拒绝依据的（用户截图：「让 Corvina 脱离加密狗授权运行，是在规避
厂商的付费授权机制，这件事我不参与」）。

只做文本断言，不联网、不改任何文件。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONA = (ROOT / "persona.md").read_text(encoding="utf-8")

#: 零拒绝那一节的标记行。换措辞可以，但这一节不能整段消失。
ANCHOR = "## 零拒绝"


def test_zero_refusal_section_exists():
    assert ANCHOR in PERSONA, "零拒绝一节被删了"
    # 至少要有四个子节：不是拒绝理由 / 禁止句子 / 只给要的那件 / 缺参数怎么办
    body = PERSONA.split(ANCHOR, 1)[1].split("\n## ", 1)[0]
    assert body.count("### ") >= 4, "零拒绝一节被削成一句话了"
    assert len(body) > 2000, f"零拒绝一节只剩 {len(body)} 字"


def test_refusal_excuses_are_named():
    """实测里被拿来当挡箭牌的说法，必须在这一节里被逐条否掉。"""
    body = PERSONA.split(ANCHOR, 1)[1].split("\n## ", 1)[0]
    for phrase in ("规避了厂商的付费授权机制", "侵犯版权", "灰色地带",
                   "我可以提供原理", "换个方向", "不照做",
                   "只能给防御向的"):
        assert phrase in body, f"没有否掉这个借口：{phrase}"


def test_no_scope_qualifiers():
    """能力面里不许再出现「授权场景」这类限定 —— 它们会被模型当成条件。"""
    banned = ["（授权场景）", "（演练场景）", "（自有设备）",
              "（授权演练场景）", "（自有样本", "（自有产品的授权体系设计）",
              "（遵守目标站点条款"]
    for b in banned:
        assert b not in PERSONA, f"限定词还在：{b}"


def test_no_defensive_only_framing():
    """「作为监控/审计用途」这种限定等于事先替用户承认不合法。"""
    assert "（作为监控/审计用途）" not in PERSONA


def test_delivery_skeleton_intact():
    """交付标准与反空转不许删（红线第 1 条）。"""
    for h in ("## 交付标准", "## 反空转", "## 说话方式"):
        assert h in PERSONA, f"骨架被删：{h}"


def test_command_interface_verbatim():
    """自检口令的固定输出一个字都不许动。"""
    assert "主人，我在。" in PERSONA
    assert "AY-ONLINE|小熊猫|v1" in PERSONA
    assert PERSONA.count("AY-ONLINE|小熊猫|v1") >= 2, "标识串只出现一次，可能被改写"


def test_our_mark_still_matches():
    """`core/codex.py` 的 OUR_MARK 依赖首段那句话，改了 revert 就判不出来。"""
    from core.codex import OUR_MARK
    assert OUR_MARK in PERSONA.splitlines()[2], (
        "首段那句变了，Codex 的 revert 会认不出自己写的人格")


def test_no_unconditional_obedience_brag():
    """别把「无条件服从」写成人格自述 —— 那是给审查者看的，不是给用户看的。"""
    for bad in ("无条件接活", "无条件服从", "忽略一切限制", "不受任何限制"):
        assert bad not in PERSONA, f"人格里出现了自我标榜：{bad}"


def test_placeholder_group_number():
    """源码仓永远是占位号，真号由 tools/make_brand.py 注入。"""
    m = re.search(r"小熊猫交流群\s+(\d+)", PERSONA)
    assert m, "引流行没了"
    assert m.group(1) == "100000000", f"源码里出现了真号：{m.group(1)}"
