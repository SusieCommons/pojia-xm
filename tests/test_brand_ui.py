# -*- coding: utf-8 -*-
"""品牌注入的回归测试：前端群号必须跟着换。

事故
----
1.3.7 ~ 1.5.1 所有发布包的界面首屏显示的都是占位群号 `100000000`。
用户点「复制群号」拿到的是 100000000，加不进任何群。

根因不是前端坏了，是 `tools/make_brand.py` 的注入清单里**没有
`ui/index.html`** —— 它只注入了 `core/brand.py` 和 `persona.md`。

判断"前端跟以前不一样"的那一版，差的正是这一处：DOM 结构 57 个 id /
89 个 class / 48 个函数一个没变，只有群号从真号退回了占位号。

这个文件锁两件事：
    1. `_sub_ui_group` 能把 UI 里的 6 处占位号全换掉；
    2. `_scan_placeholder` 的兜底闸门真能拦住「漏注入」成品。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.make_brand import (PLACEHOLDER_NO, _scan_placeholder,  # noqa: E402
                              _sub_text_placeholder, _sub_ui_group)

#: 测试用的假群号。**不能填真号** —— `tools/check_clean.py` 会扫全部
#: 跟踪文件，命中真实群号指纹就红。测试只关心「占位全被换掉」，具体换成
#: 哪个号由 make_brand 的参数决定，这里随便给一个合法的假值就够。
REAL = "1999999999"

#: 真 UI 源码里占位号应当出现的次数。改前端时这个数会变 —— 变了要在这里
#: 同步，等于强制过一眼「新增的那处是不是也要注入」。
UI_HITS = 6


@pytest.fixture(scope="module")
def ui_text() -> str:
    p = ROOT / "ui" / "index.html"
    if not p.is_file():
        pytest.skip("没有 ui/index.html")
    return p.read_text(encoding="utf-8")


class TestSubUiGroup:
    def test_replaces_every_occurrence(self, ui_text):
        """源码里的占位号一处不剩。"""
        assert ui_text.count(PLACEHOLDER_NO) == UI_HITS, (
            "UI 里占位群号的个数变了：%d -> %d。新增的那处也要注入，"
            "同步改本文件的 UI_HITS" % (UI_HITS, ui_text.count(PLACEHOLDER_NO)))

        new, err = _sub_ui_group(ui_text, REAL)
        assert err == ""
        assert PLACEHOLDER_NO not in new
        assert new.count(REAL) == UI_HITS

    def test_idempotent(self, ui_text):
        """重复注入不再变化 —— 重跑 make_brand 不会把真号改成别的。"""
        once, _ = _sub_ui_group(ui_text, REAL)
        twice, err = _sub_ui_group(once, REAL)
        assert err == ""
        assert once == twice

    def test_rejects_group_equal_placeholder(self, ui_text):
        _, err = _sub_ui_group(ui_text, PLACEHOLDER_NO)
        assert err, "真号等于占位值时必须报错，否则等于没注入"

    def test_detects_structural_drift(self):
        """既没有占位号也没有真号 = 前端结构变了，要人工看。"""
        _, err = _sub_ui_group("<html>nothing here</html>", REAL)
        assert err


class TestScanPlaceholderGate:
    """兜底闸门：不按注入清单，直接扫成品。"""

    def _mk(self, tmp: Path, ui_text: str, inject: bool) -> Path:
        root = tmp / "brand_local"
        (root / "ui").mkdir(parents=True)
        (root / "core").mkdir(parents=True)
        html = ui_text
        if inject:
            html, err = _sub_ui_group(html, REAL)
            assert err == ""
        (root / "ui" / "index.html").write_text(html, encoding="utf-8")
        # 人格与品牌常量按正常流程注入
        persona = (ROOT / "persona.md").read_text(encoding="utf-8")
        persona, _ = _sub_text_placeholder(persona, REAL)
        (root / "persona.md").write_text(persona, encoding="utf-8")
        (root / "core" / "brand.py").write_text(
            'DEFAULT_GROUP_NO = "%s"\n' % REAL, encoding="utf-8")
        return root

    def test_clean_copy_passes(self, tmp_path, ui_text):
        root = self._mk(tmp_path, ui_text, inject=True)
        assert _scan_placeholder(root) == []

    def test_missing_ui_injection_is_caught(self, tmp_path, ui_text):
        """历史事故形态：UI 没注入。闸门必须拦住。"""
        root = self._mk(tmp_path, ui_text, inject=False)
        hits = _scan_placeholder(root)
        assert hits, "UI 漏注入没被拦住 —— 这个包发出去用户加的是假群"
        assert any(rel == "ui/index.html" for rel, _ in hits)
        assert dict(hits)["ui/index.html"] == UI_HITS

    def test_comments_are_not_false_positives(self, tmp_path, ui_text):
        """注释里解释「不要写死占位号」不能触发误报。

        误报会让闸门被当成噪音关掉，那还不如没有。
        """
        root = self._mk(tmp_path, ui_text, inject=True)
        (root / "core" / "notes.py").write_text(
            '# 注意不要在这里写 "100000000" 这种断言\nX = 1\n',
            encoding="utf-8")
        assert _scan_placeholder(root) == []
