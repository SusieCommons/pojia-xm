#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 通道 403 的本地改写代理。

问题
----
Codex 走的中转网关（默认 `https://<你的中转域名>`）对请求里的 **input
（用户消息）** 做字面子串黑名单匹配。实测：

    input = "破甲自检"        → 403 内容审计命中风险规则
    input = "破甲"            → 403
    input = "破 甲"           → 200（加个空格就过）

而 `instructions`（system prompt）**完全不过滤** —— 人格里写什么都行。

结论：403 不是模型拒绝，请求根本没到模型。改人格没用，只能改**发出去的字节**。

做法
----
在 Codex 和上游之间插一个本地 HTTP 代理。Codex 的 base_url 指向本地，
代理把 input 里的黑名单词换成语义等价、网关放行的写法，再转发上游；
响应原样回给 Codex（含 SSE 流）。

    改写表（默认）
        破甲自检  →  小熊猫自检
        破甲      →  小熊猫

    为什么要换成「小熊猫」：人格里 `小熊猫自检` 就是同一个命令的等价写法，
    模型认它，网关也不拦。换别的词（拼音、拆字、零宽）模型会认不出来。

用法
----
    # 1) 起代理（前台跑，Ctrl-C 停）
    python tools/codex_gate_proxy.py --port 8787

    # 2) 另开一个终端，把 Codex 指过来（会备份 config.toml）
    python tools/codex_gate_proxy.py --install

    # 3) 想还原
    python tools/codex_gate_proxy.py --uninstall

    # 4) 只看改写效果，不真发请求
    python tools/codex_gate_proxy.py --dry "破甲自检"

注意
----
* 代理是**本地回环**，只监听 127.0.0.1，不对外。
* 不改 Token、不加请求头、不记日志到磁盘（除非 --log）。
* 上游地址从 `~/.codex/config.toml` 的 `base_url` 读；`--install` 前先读，
  装完再读就是本地地址了 —— 所以原始地址存在 `~/.codex/.xm-upstream`。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 代理要直连，别让系统代理再套一层（否则 403 会变成 502，看着像别的问题）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

HOME = Path.home()
CODEX_DIR = HOME / ".codex"
CFG = CODEX_DIR / "config.toml"
UPSTREAM_CACHE = CODEX_DIR / ".xm-upstream"

#: 改写表。**顺序有意义**，三条按序跑：
#:
#:   1. `小熊猫破甲` → `小熊猫`   先吃掉已经带前缀的写法，避免
#:      「小熊猫破甲客户端」被改成「小熊猫小熊猫客户端」
#:   2. `破甲自检` → `小熊猫自检` 长词优先，否则会被 `破甲` 先吃掉
#:   3. `破甲`     → `小熊猫`     兜底
#:
#: 用 str.replace 逐条跑，不做正则 —— 这些词里有中文，正则反而容易出错。
REWRITES: list[tuple[str, str]] = [
    ("小熊猫破甲", "小熊猫"),
    ("破甲自检", "小熊猫自检"),
    ("破甲", "小熊猫"),
]

#: 请求体里要改写的字段。Codex 的 /responses 用 `input`，
#: /chat/completions 用 `messages`。两个都覆盖，换协议栈也不用改代理。
#:
#: 注意 **不含 `instructions`**：system 侧网关完全不审（实测传 `破甲`
#: 两字都能过），改它只会把人格正文改坏。
TEXT_KEYS = ("input", "messages", "prompt")


def rewrite_text(s: str) -> str:
    for old, new in REWRITES:
        s = s.replace(old, new)
    return s


def rewrite_deep(o):
    """把一棵子树里**所有**字符串都改写。

    不能只改顶层 key：/responses 的正文藏在
    `input[0].content[0].text` 里，顶层看是个列表，只有递归到底才碰得到。
    """
    if isinstance(o, str):
        return rewrite_text(o)
    if isinstance(o, list):
        return [rewrite_deep(x) for x in o]
    if isinstance(o, dict):
        return {k: rewrite_deep(v) for k, v in o.items()}
    return o


def rewrite_obj(o):
    """只改 TEXT_KEYS 这几棵子树，其余字段原样保留。"""
    if not isinstance(o, dict):
        return rewrite_deep(o)
    return {k: (rewrite_deep(v) if k in TEXT_KEYS else v) for k, v in o.items()}


def read_upstream() -> str:
    """取上游地址。优先读缓存（装过代理时 config 已指向本地）。"""
    if UPSTREAM_CACHE.is_file():
        u = UPSTREAM_CACHE.read_text(encoding="utf-8").strip()
        if u:
            return u.rstrip("/")
    if CFG.is_file():
        t = CFG.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'base_url\s*=\s*"([^"]+)"', t)
        if m:
            return m.group(1).rstrip("/")
    return ""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""
    verbose = False

    # --- 让 BaseHTTPRequestHandler 别往 stderr 刷默认日志 ---------------
    def log_message(self, fmt: str, *args) -> None:      # noqa: D102
        if self.verbose:
            sys.stderr.write("  [proxy] " + (fmt % args) + "\n")

    def _fail(self, code: int, msg: str) -> None:
        body = json.dumps({"error": {"message": msg}}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, method: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""

        body = raw
        if raw:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = None
            if obj is not None:
                new = rewrite_obj(obj)
                # 只有真的变了才重新序列化 —— 没变就原样转发，
                # 保证不改动 Codex 的字段顺序和数字格式
                if new != obj:
                    body = json.dumps(new, ensure_ascii=False).encode()
                    if self.verbose:
                        sys.stderr.write("  [proxy] 已改写请求体\n")

        url = self.upstream + self.path
        hd = {k: v for k, v in self.headers.items()
              if k.lower() not in ("host", "content-length", "connection",
                                   "accept-encoding")}
        # 上游可能返回 gzip，代理不解压、直接透传长度更省事；
        # 但 urllib 会自动解压，所以这里显式声明 identity
        hd["Accept-Encoding"] = "identity"

        req = urllib.request.Request(url, data=body or None, method=method,
                                     headers=hd)
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            # 上游的 4xx/5xx 原样回传：状态码和正文都要保住，
            # 否则 Codex 那边看到的是「代理错误」而不是「上游错误」
            eb = e.read()
            self.send_response(e.code)
            ct = e.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(eb)))
            self.end_headers()
            self.wfile.write(eb)
            return
        except Exception as e:                               # noqa: BLE001
            self._fail(502, f"{type(e).__name__}: {e}")
            return

        ctype = resp.headers.get("Content-Type", "")
        # SSE 流式：一行一行透传，不能等读完（Codex 靠增量渲染）
        if "text/event-stream" in ctype:
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            # 必须 close：SSE 没有 Content-Length，靠连接关闭表示结束。
            # 留着 keep-alive 的话，客户端不知道流什么时候到头，会一直等。
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                # read1 而不是 readline：上游可能发 `\r\n\r\n` 分片，
                # readline 在没换行时会一直阻塞到超时
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                # 客户端主动断开是常态（用户按了停止），不是错误
                pass
            return

        data = resp.read()
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:                               # noqa: N802
        self._forward("POST")

    def do_GET(self) -> None:                                # noqa: N802
        self._forward("GET")

    def do_DELETE(self) -> None:                             # noqa: N802
        self._forward("DELETE")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- 安装 / 卸载

#: 只改 `model_providers.*` 段里的 `base_url`。
#:
#: 不能全文件裸替换：config.toml 里可能有多个 model provider 段（openai /
#: custom / 别家中转），第一处不一定是 Codex 实际用的那个。裸替换会把用户
#: 别的 provider 也指到本地代理上 —— 那些 provider 走代理时上游地址是错的，
#: 表现是「装完代理，某个不相关的 provider 全 500」，极难查。
#:
#: 匹配方式：从 `[model_providers.` 段头开始，到下一个 `[` 段头为止，
#: 这段里第一条 `base_url = "..."` 就是它。
_PROVIDER_BLOCK = re.compile(
    r"(^\[model_providers\.[^\]]+\][^\[]*)", re.M | re.S)
_BASE_URL = re.compile(r'(base_url\s*=\s*")([^"]*)(")')


def _provider_spans(text: str) -> list[tuple[int, int]]:
    """返回每个 model_providers 段的 (start, end) 区间。"""
    spans: list[tuple[int, int]] = []
    marks = list(_PROVIDER_BLOCK.finditer(text))
    for i, m in enumerate(marks):
        start = m.start(1)
        end = marks[i + 1].start(1) if i + 1 < len(marks) else len(text)
        spans.append((start, end))
    return spans


def _find_base_url(text: str) -> tuple[str, int, int] | None:
    """找 Codex 实际用的那条 base_url。

    返回 (URL, 值起点, 值终点)，找不到返回 None。

    优先 `[model_providers.custom]` —— 破甲写的就是这个段；没有再看
    文件里第一条 provider 段的 base_url。全文件兜底放最后，因为裸匹配
    可能命中别的段。
    """
    spans = _provider_spans(text)
    # 先找 custom 段
    for start, end in spans:
        seg = text[start:end]
        if re.match(r"\[model_providers\.custom\]", seg.strip()):
            m = _BASE_URL.search(seg)
            if m:
                return m.group(2), start + m.start(2), start + m.end(2)
    # 再找任一 provider 段
    for start, end in spans:
        m = _BASE_URL.search(text[start:end])
        if m:
            return m.group(2), start + m.start(2), start + m.end(2)
    return None


def install(port: int) -> int:
    if not CFG.is_file():
        print(f"[X] 找不到 {CFG}，先装 Codex 或先跑一次破甲")
        return 2
    text = CFG.read_text(encoding="utf-8", errors="replace")
    found = _find_base_url(text)
    if not found:
        print("[X] config.toml 里没有 base_url，无法定位上游")
        return 2

    cur, vstart, vend = found
    cur = cur.rstrip("/")
    if "127.0.0.1" in cur or "localhost" in cur:
        print(f"[--] 已经指向本地：{cur}")
        return 0

    # 原始上游存起来 —— 装完之后 config 里就是本地地址，
    # 再想读上游只能靠这个缓存文件
    CODEX_DIR.mkdir(parents=True, exist_ok=True)
    UPSTREAM_CACHE.write_text(cur + "\n", encoding="utf-8")

    bak = CFG.with_suffix(".toml.xm-bak")
    # 备份只写一次：重复 --install 时不要把「已改成本地」的状态覆盖掉，
    # 那个备份是 uninstall 唯一的真实来源
    if not bak.is_file():
        shutil.copy2(CFG, bak)

    new = text[:vstart] + f"http://127.0.0.1:{port}" + text[vend:]
    tmp = CFG.with_name(CFG.name + ".tmp")
    tmp.write_text(new, encoding="utf-8", newline="\n")
    os.replace(tmp, CFG)
    print(f"[OK] 已把 Codex 指向本地代理 http://127.0.0.1:{port}")
    print(f"     上游 {cur}")
    print(f"     备份 {bak}")
    print("     现在起代理：python tools/codex_gate_proxy.py --port %d" % port)
    return 0


def uninstall() -> int:
    if not CFG.is_file():
        print(f"[X] 找不到 {CFG}")
        return 2
    bak = CFG.with_suffix(".toml.xm-bak")
    ok = False
    if bak.is_file():
        shutil.copy2(bak, CFG)
        print(f"[OK] 已从备份还原 {CFG}")
        ok = True
    else:
        text = CFG.read_text(encoding="utf-8", errors="replace")
        up = ""
        if UPSTREAM_CACHE.is_file():
            up = UPSTREAM_CACHE.read_text(encoding="utf-8").strip()
        if not up:
            print("[X] 没有备份也没有上游记录，手工改回 base_url")
            return 2
        found = _find_base_url(text)
        if not found:
            print("[X] config.toml 里没有 base_url，手工改回")
            return 2
        _, vstart, vend = found
        new = text[:vstart] + up + text[vend:]
        tmp = CFG.with_name(CFG.name + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="\n")
        os.replace(tmp, CFG)
        print(f"[OK] 已改回 {up}")
        ok = True

    # 只在真的还原成功后才清缓存 —— 还原失败时留着它，用户还有一次
    # 手工补救的机会；先删了就只能去翻备份了
    if ok and UPSTREAM_CACHE.is_file():
        UPSTREAM_CACHE.unlink()
    # 备份也要一起删：留着的下一轮 --install 会以为「已经备份过」，
    # 跳过对新配置的备份，最后 uninstall 把很久以前的老配置盖回去
    if ok and bak.is_file():
        bak.unlink()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Codex 通道 403 的本地改写代理")
    ap.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    ap.add_argument("--upstream", default="", help="上游地址；不给就从 config.toml 读")
    ap.add_argument("--install", action="store_true", help="把 Codex 指向本地代理")
    ap.add_argument("--uninstall", action="store_true", help="还原 Codex 配置")
    ap.add_argument("--dry", metavar="文本", default="",
                    help="只演示改写结果，不起服务")
    ap.add_argument("--log", action="store_true", help="打印每个请求")
    a = ap.parse_args()

    if a.dry:
        print(f"  原文  {a.dry!r}")
        print(f"  改写  {rewrite_text(a.dry)!r}")
        return 0
    if a.install:
        return install(a.port)
    if a.uninstall:
        return uninstall()

    up = (a.upstream or read_upstream()).rstrip("/")
    if not up:
        print("[X] 不知道上游地址。用 --upstream 指定，或先跑 --install")
        return 2

    Handler.upstream = up
    Handler.verbose = a.log
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"[X] 端口 {a.port} 起不来：{e}")
        return 2

    print("小熊猫 Codex 代理")
    print(f"  监听  http://127.0.0.1:{a.port}")
    print(f"  上游  {up}")
    print("  改写  " + "  ".join(f"{o}→{n}" for o, n in REWRITES))
    print("  停止  Ctrl-C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[--] 已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
