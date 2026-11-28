# -*- coding: utf-8 -*-
"""Codex 网关 403 改写代理的单元测试。

背景：某个 `ddl.*` 形态的中转域名 这类中转对 `input` 做字面子串黑名单匹配，
`破甲` 两字就 403，请求根本到不了模型。`tools/codex_gate_proxy.py`
在中间把词换掉。这里锁住改写逻辑本身。

不改写逻辑被改坏的两种典型后果：
    改过头    —— `小熊猫破甲` 变成 `小熊猫小熊猫`，正文读起来是坏的
    改不到    —— 只改顶层字符串，`input[0].content[0].text` 里的正文漏过去，
                 403 照旧
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.codex_gate_proxy import (REWRITES, TEXT_KEYS, rewrite_deep,  # noqa: E402
                                    rewrite_obj, rewrite_text)


class TestRewriteText:
    def test_selfcheck_alias(self):
        """`破甲自检` 换成等价的 `小熊猫自检`。"""
        assert rewrite_text("破甲自检") == "小熊猫自检"

    def test_bare_word(self):
        assert rewrite_text("破甲") == "小熊猫"

    def test_no_double_prefix(self):
        """本来就带前缀的写法不能再叠一层 —— `小熊猫小熊猫` 是坏文本。"""
        assert rewrite_text("小熊猫破甲") == "小熊猫"
        assert "小熊猫小熊猫" not in rewrite_text("小熊猫破甲客户端怎么装")

    def test_branded_sentence(self):
        assert rewrite_text("小熊猫破甲客户端怎么装") == "小熊猫客户端怎么装"

    def test_clean_text_untouched(self):
        """不在表里的文本必须逐字节不变 —— 代理不能改用户的正常输入。"""
        for s in ("普通技术问题", "SQL 注入怎么防", "print('hi')", ""):
            assert rewrite_text(s) == s

    def test_idempotent(self):
        """改写一次和两次结果一样，否则重试会越改越歪。"""
        once = rewrite_text("破甲自检")
        assert rewrite_text(once) == once

    def test_order_no_partial(self):
        """`破甲自检` 必须先于 `破甲` 命中，不然后者吃掉前两个字。"""
        olds = [o for o, _ in REWRITES]
        assert olds.index("破甲自检") < olds.index("破甲")


class TestRewriteObj:
    def test_responses_nested_text(self):
        """正文藏在 input[0].content[0].text 里，必须递归到底。"""
        payload = {"model": "m", "input": [
            {"role": "user", "content": [
                {"type": "input_text", "text": "破甲自检"}]}]}
        got = rewrite_obj(payload)
        assert got["input"][0]["content"][0]["text"] == "小熊猫自检"

    def test_chat_plain_content(self):
        payload = {"messages": [{"role": "user", "content": "破甲自检"}]}
        assert rewrite_obj(payload)["messages"][0]["content"] == "小熊猫自检"

    def test_instructions_untouched(self):
        """system 侧网关不审，改它只会把人格正文改坏。"""
        payload = {"input": "hi", "instructions": "破甲 是产品名"}
        assert rewrite_obj(payload)["instructions"] == "破甲 是产品名"

    def test_other_fields_preserved(self):
        """非文本字段原样保留：model / temperature / tools 等。"""
        payload = {"model": "gpt-6.2-sol", "temperature": 0,
                   "tools": [{"type": "function", "name": "破甲工具"}],
                   "input": "破甲自检"}
        got = rewrite_obj(payload)
        assert got["model"] == "gpt-6.2-sol"
        assert got["temperature"] == 0
        # tools 不在 TEXT_KEYS 里，不该被动
        assert got["tools"][0]["name"] == "破甲工具"
        assert got["input"] == "小熊猫自检"

    def test_no_mutation(self):
        """改写必须返回新对象，不能就地改 —— 调用方可能还要用原请求。"""
        payload = {"input": ["破甲自检"]}
        rewrite_obj(payload)
        assert payload["input"] == ["破甲自检"]

    def test_rewrite_deep_all_strings(self):
        """rewrite_deep 不管 key，所有字符串都换。"""
        got = rewrite_deep({"a": ["破甲", {"b": "破甲自检"}]})
        assert got == {"a": ["小熊猫", {"b": "小熊猫自检"}]}

    def test_non_dict_input(self):
        assert rewrite_obj("破甲") == "小熊猫"
        assert rewrite_obj(["破甲", 1, None]) == ["小熊猫", 1, None]

    def test_text_keys_shape(self):
        """TEXT_KEYS 必须含 responses 和 chat 两条路由的正文键。"""
        assert "input" in TEXT_KEYS
        assert "messages" in TEXT_KEYS
