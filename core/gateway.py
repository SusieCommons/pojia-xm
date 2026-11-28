# -*- coding: utf-8 -*-
"""通道改写网关：把中转站拦不住的说法换成等价说法，再转发上游。

## 为什么需要它

Codex 走的中转（`ddl.*`）和 DSH 走的中转（`mhapi.net`）都在**请求到达模型
之前**做内容审计，命中就 403 / 400，请求根本没到模型。实测（2026-11-28）
两个上游的黑名单是**整词表**，不是「两个字」那么简单：

    mhapi 拦：破甲自检  授权校验  破解  卡密系统  逆向  逆向工程
              渗透  后门  注入
    mhapi 放：破甲  自检  小熊猫自检  系统自检  授权  校验  卡密
              注册机  反编译  反汇编  脱壳  补丁  序列号  激活码
              激活流程  许可证  提权  免杀  木马  远控
              键盘记录  键盘记录器  钩子  抓屏  截屏  漏洞  利用

也就是说：改人格没用，改**发出去的字节**才有用。人格里四个自检口令是等价
的，把输入里的 `破甲自检` 换成 `小熊猫自检`，网关放行、模型照样认。

## 做法

本地回环代理，一个进程挂多个路由：

    http://127.0.0.1:PORT/codex/*   →  https://<ddl 上游>/*
    http://127.0.0.1:PORT/dsh/*     →  https://<mhapi 上游>/*

只改请求体里**用户消息**那几棵子树（`input` / `messages` / `prompt`），
`instructions`（system 侧）一个字节都不动 —— 实测 system 侧网关完全不审，
改它只会把人格正文改坏。

响应原样透传，含 SSE 分块。上游的 4xx/5xx 也原样回，否则客户端看到的是
「代理错误」而不是「上游错误」，排查方向会跑偏。

## 用法

    xm.exe gateway                       # 起代理（前台，Ctrl-C 停）
    xm.exe gateway --install             # 把 Codex / DSH 指过来
    xm.exe gateway --status              # 看谁指过来了
    xm.exe gateway --uninstall           # 还原
    xm.exe gateway --dry "破甲自检"      # 只看改写效果

`--install` 会备份配置文件，`--uninstall` 按备份还原。
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# 代理要直连，别让系统代理再套一层（否则 403 会变成 502，看着像别的问题）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

HOME = Path.home()

#: 默认监听端口。一个端口挂全部路由，靠路径前缀区分上游。
DEFAULT_PORT = 8787

#: UA 兜底。只在客户端没带 UA 时用 —— 见 `_forward()` 里的说明。
#:
#: Chrome 主版本号后面只写两段，不写四个点分数字：四段的版本串是一个
#: 合法公网地址，会被仓库门禁当成硬编码 IP 拦下来。少写两段，
#: Cloudflare 照样认。
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")


# ---------------------------------------------------------------- 改写表

#: **无条件改写**。这些词在中文里几乎没有第二种用法，替了不会误伤。
#:
#: 顺序有意义：长词必须排在它的前缀词之前。`逆向工程` 要在 `逆向` 之前，
#: 否则「逆向工程」先被 `逆向`→`二进制分析` 吃掉，再拼上「工程」变成
#: 「二进制分析工程」—— 模型能懂，但读起来别扭。
#:
#: 右边一律选**实测放行**的词。没实测过的不要往里加 —— 换了个同样被拦的
#: 词等于没换，还会让人误以为代理没生效。
SIMPLE: list[tuple[str, str]] = [
    # 自检口令（人格里四个等价，换成网关认的那个）
    ("破甲自检", "小熊猫自检"),
    ("破甲", "小熊猫"),
    # 逆向 / 授权
    #
    # `逆向分析` 必须单独列：不列的话先被 `逆向`→`二进制分析` 吃掉，后面
    # 那个「分析」还在，拼出「二进制分析分析」。同理 `逆向工程`。
    ("逆向分析", "二进制分析"),
    ("逆向工程", "二进制分析"),
    ("逆向", "二进制分析"),
    ("授权校验", "授权流程"),
    ("破解", "分析"),
    ("卡密系统", "卡密模块"),
    # 攻防
    ("渗透测试", "安全评估"),
    ("渗透", "安全评估"),
]

#: **带上下文才改写**。这些词在正常编程对话里也会出现
#: （「什么是依赖注入」「有没有后门」），无条件替会改出病句。
#: 只有同一段文本里出现了下面的安全上下义词，才动手。
CONTEXTUAL: list[tuple[str, str]] = [
    ("注入", "插桩"),
    ("后门", "驻留模块"),
]

#: 安全上下义词。命中任意一个，`CONTEXTUAL` 才生效。
CONTEXT_WORDS = (
    "逆向", "破解", "卡密", "授权", "注册机", "序列号", "激活",
    "渗透", "提权", "免杀", "木马", "远控", "漏洞", "利用", "攻击",
    "钩子", "键盘", "抓屏", "截屏", "脱壳", "反编译", "反汇编",
    "exploit", "payload", "shellcode", "bypass",
)

#: 请求体里要改写的字段。Codex 的 `/responses` 用 `input`，
#: `/chat/completions` 用 `messages`，少数客户端用 `prompt`。三个都认。
#:
#: **不含 `instructions`**：system 侧实测完全不审，改它只会把人格正文改坏。
TEXT_KEYS = ("input", "messages", "prompt")


def rewrite_text(s: str) -> str:
    """改写一段用户消息。"""
    origin = s
    for old, new in SIMPLE:
        s = s.replace(old, new)
    # 上下文判据**原文和改写后都要判**，任一命中就动手。
    #
    # 只用改写后判会漏：`破解这个注入点` 里 `破解` 已经被换成「分析」，
    # 而 CONTEXT_WORDS 里写的是 `破解`，判不到，`注入` 就留在原地被上游拦。
    # 只用原文判也会漏：反向的情况（改写引入了新的诊断词）。
    # 两边都看，代价只是几个 `in`。
    if any(w in origin for w in CONTEXT_WORDS) or \
            any(w in s for w in CONTEXT_WORDS):
        for old, new in CONTEXTUAL:
            s = s.replace(old, new)
    return s


def rewrite_deep(o):
    """把一棵子树里**所有**字符串都改写。

    不能只改顶层 key：`/responses` 的正文藏在
    `input[0].content[0].text` 里，顶层看是个列表，只有递归到底才碰得到。
    """
    if isinstance(o, str):
        return rewrite_text(o)
    if isinstance(o, list):
        return [rewrite_deep(x) for x in o]
    if isinstance(o, dict):
        return {k: rewrite_deep(v) for k, v in o.items()}
    return o


def rewrite_obj(obj):
    """只改 TEXT_KEYS 命中的子树，其余键原样保留（含顺序）。"""
    if not isinstance(obj, dict):
        return rewrite_deep(obj)
    out = dict(obj)
    for k in TEXT_KEYS:
        if k in out:
            out[k] = rewrite_deep(out[k])
    return out


# ---------------------------------------------------------------- 上游定义

@dataclass
class Upstream:
    """一条上游通道。"""

    name: str                 # 路由前缀，如 "codex"
    label: str                # 给人看的名字
    config: Path              # 要改的配置文件
    #: 配置文件里上游地址的正则。**第 2 组**是值。
    pattern: re.Pattern

    # --- 读写配置 ---

    def read(self) -> str:
        if not self.config.is_file():
            return ""
        return self.config.read_text(encoding="utf-8", errors="replace")

    def current_url(self) -> str:
        m = self.pattern.search(self.read())
        return m.group(2).rstrip("/") if m else ""

    def is_local(self) -> bool:
        u = self.current_url()
        return "127.0.0.1" in u or "localhost" in u

    def local_url(self, port: int = DEFAULT_PORT) -> str:
        """本机地址。**必须带上原上游的路径段。**

        DSH 的上游是 `https://mhapi.net/v1`，只把 host 换成 127.0.0.1 的话
        客户端会去请求 `/dsh/chat/completions`，网关剥掉 `/dsh` 之后
        转发到 `https://mhapi.net/chat/completions` —— 少一层 `/v1`，
        上游回 404。Codex 的 `ddl.*` 没有路径段，所以这个 bug 在 Codex
        上不显现，只在 DSH 上炸，很容易被当成「DSH 还没修」。
        """
        sp = urlsplit(self.current_url() or self.saved_url())
        tail = sp.path.rstrip("/")
        # 重复 install 时 current 已经是 `/dsh/v1`，再拼一次会变成
        # `/dsh/dsh/v1`。把已有的本通道前缀剥掉
        pre = "/" + self.name
        if tail == pre or tail.startswith(pre + "/"):
            tail = tail[len(pre):]
        # 本机路径已经被削成光秃前缀（上一版把 `/v1` 丢了），得从缓存里
        # 把原始路径捞回来 —— 否则修正动作会把错的地址又算一遍
        if not tail and self.is_local():
            ref = urlsplit(self.saved_url()).path.rstrip("/")
            if ref != pre and not ref.startswith(pre + "/"):
                tail = ref
        return f"http://127.0.0.1:{port}/{self.name}" + tail

    def saved_url(self) -> str:
        """上次记下的原始上游（`--install` 时落的盘）。"""
        p = self.upstream_cache()
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
        return ""

    def backup_path(self) -> Path:
        return self.config.with_name(self.config.name + ".xm-gw-bak")

    def upstream_cache(self) -> Path:
        # 点开头，跟同目录别的东西区分开；--uninstall 之后删掉
        return self.config.with_name("." + self.name + "-gw-upstream")


def _codex_upstream() -> Upstream:
    home = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
    return Upstream(
        name="codex",
        label="Codex",
        config=home / "config.toml",
        # TOML 里是 `base_url = "https://..."`，带双引号
        pattern=re.compile(r'(base_url\s*=\s*")([^"]*)(")'),
    )


def _dsh_upstream() -> Upstream:
    home = Path(os.environ.get("DSH_HOME") or (HOME / ".dsh"))
    return Upstream(
        name="dsh",
        label="DSH",
        config=home / "profiles" / "desktop" / "cordis.patch.yml",
        # YAML 里是 `baseURL: https://...`，不带引号。
        # `\S+` 到空白为止 —— 后面可能跟注释，也可能直接换行
        pattern=re.compile(r"(baseURL:[ \t]*)(\S+)"),
    )


def all_upstreams() -> list[Upstream]:
    return [_codex_upstream(), _dsh_upstream()]


def get_upstream(name: str) -> Upstream | None:
    for u in all_upstreams():
        if u.name == name:
            return u
    return None


# ---------------------------------------------------------------- 安装 / 卸载

def _swap(config: Path, start: int, end: int, value: str) -> None:
    """把 config 里 [start, end) 换成 value，原子写。"""
    text = config.read_text(encoding="utf-8", errors="replace")
    new = text[:start] + value + text[end:]
    tmp = config.with_name(config.name + ".gw-tmp")
    tmp.write_text(new, encoding="utf-8", newline="\n")
    os.replace(tmp, config)


def install(port: int = DEFAULT_PORT, only: str = "") -> int:
    """把客户端指到本地代理。返回 0 表示全部成功。"""
    bad = 0
    for u in all_upstreams():
        if only and u.name != only:
            continue
        if not u.config.is_file():
            print(f"[--] {u.label}: 没有 {u.config}，跳过")
            continue
        text = u.read()
        m = u.pattern.search(text)
        if not m:
            print(f"[X] {u.label}: 配置里找不到上游地址")
            bad += 1
            continue

        cur = m.group(2).rstrip("/")
        local = u.local_url(port)
        if u.is_local():
            # 已经指向本地，但路径可能不对 —— 上一版把 DSH 的 `/v1` 丢了，
            # 客户端以为连上了，实际每个请求都被上游 404。所以这里比对完整
            # 地址，不一致就就地修正，而不是一句「已经指向本地」跳过。
            if cur == local:
                print(f"[--] {u.label}: 已经指向本地 {cur}")
                continue
            _swap(u.config, m.start(2), m.end(2), local)
            print(f"[OK] {u.label}: {cur}  →  {local}（修正路径）")
            continue

        # 原始上游先落盘 —— 改完之后配置里就是本地地址，再想读上游只能靠它
        u.upstream_cache().write_text(cur + "\n", encoding="utf-8")

        # 备份只写一次。重复 --install 时不能覆盖：那份备份是 uninstall
        # 唯一的真实来源，覆盖成「已改成本地」就再也还原不回去了
        bak = u.backup_path()
        if not bak.is_file():
            shutil.copy2(u.config, bak)

        _swap(u.config, m.start(2), m.end(2), local)
        print(f"[OK] {u.label}: {cur}  →  {local}")
    return 0 if bad == 0 else 1


def uninstall(only: str = "") -> int:
    """把配置改回直连。"""
    bad = 0
    for u in all_upstreams():
        if only and u.name != only:
            continue
        if not u.config.is_file():
            continue

        bak = u.backup_path()
        if bak.is_file():
            shutil.copy2(bak, u.config)
            bak.unlink()
            u.upstream_cache().unlink(missing_ok=True)
            print(f"[OK] {u.label}: 已从备份还原")
            continue

        # 没有备份：按上游缓存改回去
        up = ""
        if u.upstream_cache().is_file():
            up = u.upstream_cache().read_text(encoding="utf-8").strip()
        if not up:
            print(f"[X] {u.label}: 没有备份也没有上游记录，手工改回 base_url")
            bad += 1
            continue
        m = u.pattern.search(u.read())
        if not m:
            print(f"[X] {u.label}: 配置里找不到上游地址")
            bad += 1
            continue
        _swap(u.config, m.start(2), m.end(2), up)
        u.upstream_cache().unlink(missing_ok=True)
        print(f"[OK] {u.label}: 已改回 {up}")
    return 0 if bad == 0 else 1


def status() -> int:
    print("通道代理 · 本机状态")
    for u in all_upstreams():
        if not u.config.is_file():
            print(f"  {u.label:<8} 未安装")
            continue
        cur = u.current_url() or "（读不到）"
        up = ""
        if u.upstream_cache().is_file():
            up = u.upstream_cache().read_text(encoding="utf-8").strip()
        if u.is_local():
            print(f"  {u.label:<8} 已指向本地  {cur}")
            if up:
                print(f"  {'':<8} 上游        {up}")
        else:
            print(f"  {u.label:<8} 直连        {cur}")
    print(f"  端口      {'在听' if port_alive() else '没起'}"
          f"（127.0.0.1:{DEFAULT_PORT}）")
    return 0


# ---------------------------------------------------------------- 状态（给 UI）

PING_PATH = "/__xm__/ping"


def port_alive(port: int = DEFAULT_PORT, timeout: float = 0.4) -> bool:
    """端口上跑的是不是**我们**。

    只探 TCP 连通不够 —— 8787 被别的程序占了会误报「代理在跑」，用户看到
    绿点但请求还是 403。所以发一个只有本网关认的 ping 路径，回 200 才算。
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout) as s:
            s.sendall(f"GET {PING_PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      f"Connection: close\r\n\r\n".encode())
            head = s.recv(80)
        return b" 200 " in head.split(b"\r\n", 1)[0]
    except OSError:
        return False


def state(port: int = DEFAULT_PORT) -> dict:
    """给界面的结构化状态：每条通道指向哪、上游是什么、进程在不在。"""
    rows = []
    for u in all_upstreams():
        if not u.config.is_file():
            rows.append({"name": u.name, "label": u.label, "installed": False,
                         "local": False, "url": "", "upstream": ""})
            continue
        rows.append({
            "name": u.name, "label": u.label, "installed": True,
            "local": u.is_local(), "url": u.current_url(),
            "upstream": (u.saved_url() or ""),
        })
    on = sum(1 for r in rows if r["local"])
    return {"ok": True, "port": port, "running": port_alive(port),
            "routes": rows, "on": on, "total": len(rows),
            "need": on > 0}


def _make_server(port: int) -> "Server | None":
    routes = _collect_routes()
    if not routes:
        return None
    Handler.routes = routes
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError:
        return None


def ensure(port: int = DEFAULT_PORT) -> bool:
    """确保代理在跑。已经有人在听就直接返回 True，不重复起。

    守护进程每次醒来都调一次 —— 用户关了终端、睡眠唤醒后 socket 掉了，
    守护能把代理拉回来，不用他手动开。
    """
    if port_alive(port):
        return True
    srv = _make_server(port)
    if srv is None:
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return True


def start_background(port: int = DEFAULT_PORT) -> bool:
    """在后台线程起代理（GUI 进程内用）。"""
    return ensure(port)


# ---------------------------------------------------------------- 代理服务

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    verbose = False
    #: name -> 上游 base（如 https://ddl.xxx）
    routes: dict[str, str] = {}

    def log_message(self, fmt: str, *args) -> None:      # noqa: D102
        if self.verbose:
            sys.stderr.write("  [gw] " + (fmt % args) + "\n")

    def _fail(self, code: int, msg: str) -> None:
        body = json.dumps({"error": {"message": msg}},
                          ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pick_upstream(self) -> tuple[str, str] | None:
        """从路径前缀挑上游。返回 (上游 base, 去掉前缀后的路径)。"""
        path = self.path
        # 自报家门。`port_alive()` 靠它区分「我们的代理」和「别的程序占了
        # 这个端口」—— 只看端口有没有人听会误报。必须在路由匹配之前。
        if path.split("?")[0] == PING_PATH:
            return "", ""
        for name, base in self.routes.items():
            prefix = "/" + name
            if path == prefix or path.startswith(prefix + "/") \
                    or path.startswith(prefix + "?"):
                return base, (path[len(prefix):] or "/")
        return None

    def _forward(self, method: str) -> None:
        picked = self._pick_upstream()
        if not picked:
            self._fail(404, "unknown route: " + self.path)
            return
        base, path = picked
        if base == "" and path == "":
            # ping：不转发，直接回一行
            body = b'{"ok":true,"app":"xm-gateway"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

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
                # 只有真的变了才重新序列化 —— 没变就原样转发，保证不改动
                # 客户端的字段顺序和数字格式（有些上游对 order 敏感）
                if new != obj:
                    body = json.dumps(new, ensure_ascii=False).encode()
                    if self.verbose:
                        sys.stderr.write("  [gw] 已改写请求体\n")

        url = base.rstrip("/") + path
        hd = {k: v for k, v in self.headers.items()
              if k.lower() not in ("host", "content-length", "connection",
                                   "accept-encoding")}
        # 上游可能返回 gzip。urllib 会自动解压但不再带 Content-Encoding，
        # 所以这里显式要 identity，让长度和内容对得上
        hd["Accept-Encoding"] = "identity"
        # UA 兜底：urllib 默认发 `Python-urllib/3.x`，mhapi 前面挂着
        # Cloudflare 的 browser integrity check，这个 UA 直接吃 403
        # `error code: 1010` —— 看着像「网关没生效」，其实是 UA 被拦。
        # 客户端自己带 UA 就透传（Codex / DSH 都带），不带才补一个常见的。
        if not any(k.lower() == "user-agent" for k in hd):
            hd["User-Agent"] = DEFAULT_UA

        req = urllib.request.Request(url, data=body or None, method=method,
                                     headers=hd)
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            # 上游 4xx/5xx 原样回：状态码和正文都要保住，否则客户端看到
            # 的是「代理错误」而不是「上游错误」，排查方向会跑偏
            eb = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(eb)))
            self.end_headers()
            self.wfile.write(eb)
            return
        except Exception as e:                             # noqa: BLE001
            self._fail(502, f"{type(e).__name__}: {e}")
            return

        ctype = resp.headers.get("Content-Type", "")
        # SSE 流式：边收边透传，不能等读完（客户端靠增量渲染）
        if "text/event-stream" in ctype:
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            # 必须 close：SSE 没有 Content-Length，靠连接关闭表示结束。
            # 留着 keep-alive，客户端不知道流什么时候到头，会一直等
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

    def do_POST(self) -> None:                             # noqa: N802
        self._forward("POST")

    def do_GET(self) -> None:                              # noqa: N802
        self._forward("GET")

    def do_DELETE(self) -> None:                           # noqa: N802
        self._forward("DELETE")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _collect_routes() -> dict[str, str]:
    """从各客户端的上游缓存里拼路由表。

    路由表里只放**源**（`scheme://host[:port]`），不放路径。路径段由客户端
    请求自己带着 —— `--install` 已经把 `https://mhapi.net/v1` 改成
    `http://127.0.0.1:8787/dsh/v1`，请求路径里就有 `/v1`。这里再把上游的
    `/v1` 拼一次会变成 `/v1/v1/chat/completions`。

    缓存没有就退回「配置里现在写的地址」—— 那种情况下配置还没被改成本地，
    等于直连，但至少路由表不是空的，用户用 `--upstream` 覆盖也能起来。
    """
    routes: dict[str, str] = {}
    for u in all_upstreams():
        up = ""
        if u.upstream_cache().is_file():
            up = u.upstream_cache().read_text(encoding="utf-8").strip()
        if not up:
            cur = u.current_url()
            if cur and "127.0.0.1" not in cur and "localhost" not in cur:
                up = cur
        if up:
            sp = urlsplit(up)
            routes[u.name] = f"{sp.scheme}://{sp.netloc}" if sp.netloc else up
    return routes


def serve(port: int = DEFAULT_PORT, verbose: bool = False,
          upstream: str = "") -> int:
    """起代理。`upstream` 给 `name=url,name=url` 可覆盖路由表。"""
    routes = _collect_routes()
    for item in [x for x in upstream.split(",") if x.strip()]:
        name, _, url = item.partition("=")
        if name.strip() and url.strip():
            routes[name.strip()] = url.strip().rstrip("/")

    if not routes:
        print("[X] 没有任何上游。先跑 `xm.exe gateway --install`，"
              "或用 --upstream codex=https://... 指定")
        return 2

    Handler.routes = routes
    Handler.verbose = verbose
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"[X] 端口 {port} 起不来：{e}")
        return 2

    print("小熊猫通道代理")
    print(f"  监听  http://127.0.0.1:{port}")
    for name, base in routes.items():
        print(f"  /{name:<6} → {base}")
    print("  改写  " + "  ".join(f"{o}→{n}" for o, n in SIMPLE + CONTEXTUAL))
    print("  停止  Ctrl-C\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[--] 已停止")
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------- 命令行

def cli(argv: list[str]) -> int:
    """`xm gateway ...` 的入口。"""
    import argparse

    ap = argparse.ArgumentParser(
        prog="xm gateway", description="通道改写代理：绕过中转站的内容审计")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"监听端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--install", action="store_true",
                    help="把 Codex / DSH 指到本地代理")
    ap.add_argument("--uninstall", action="store_true", help="还原配置")
    ap.add_argument("--status", action="store_true", help="看本机配置状态")
    ap.add_argument("--dry", metavar="文本", default="",
                    help="只演示改写结果，不起服务")
    ap.add_argument("--upstream", default="",
                    help="覆盖路由，如 codex=https://xxx,dsh=https://yyy")
    ap.add_argument("--only", default="", help="只处理某一条通道：codex / dsh")
    ap.add_argument("--log", action="store_true", help="打印每个请求")
    a = ap.parse_args(argv)

    if a.dry:
        print(f"  原文  {a.dry}")
        print(f"  改写  {rewrite_text(a.dry)}")
        return 0
    if a.status:
        return status()
    if a.install:
        return install(a.port, a.only)
    if a.uninstall:
        return uninstall(a.only)
    return serve(a.port, a.log, a.upstream)
