#!/usr/bin/env python3
"""小熊猫破甲 —— 自动识别本机 AI 客户端，一键注入统一人格。

    python xm.py                  打开窗口（默认）
    python xm.py status           只看状态
    python xm.py apply            给所有已装客户端注入
    python xm.py apply -t codex   只注入 Codex
    python xm.py revert           还原到注入前
    python xm.py apply --dry      只算不写

数据目录：~/.xiaoxiongmao/（备份 + 状态）
"""
from __future__ import annotations

import argparse
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import LABELS, ORDER, all_targets, resolve  # noqa: E402
from core.zh_argparse import apply_zh  # noqa: E402

TITLE = "小熊猫破甲"
SUB = "自动识别已装的 AI 客户端，选一个注入"


def load_persona(path: str | None = None) -> str:
    p = Path(path) if path else (HERE / "persona.md")
    if not p.exists():
        raise SystemExit(f"找不到人格文件：{p}")
    raw = p.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- CLI

def cmd_status() -> int:
    print(f"{TITLE} · 本机客户端")
    for t in all_targets():
        print(f"  {LABELS[t.name]:<12} {t.status()}")
    return 0


def cmd_apply(names, persona_path, dry: bool) -> int:
    persona = load_persona(persona_path)
    targets = resolve(names)
    if not targets:
        print("没有匹配的目标")
        return 1
    ok = 0
    for t in targets:
        rep = t.inject(persona, dry=dry)
        print("  " + rep.line())
        for s in rep.skipped[:5]:
            print(f"      · {s}")
        if rep.ok and rep.changed:
            ok += 1
    print(f"\n{dry and '预演' or '完成'}：{ok}/{len(targets)} 个目标已处理")
    print("重启客户端后生效")
    return 0 if ok or dry else 1


def cmd_revert(names) -> int:
    targets = resolve(names)
    n = 0
    for t in targets:
        rep = t.revert()
        print("  " + rep.line())
        if rep.ok:
            n += 1
    print(f"\n已还原 {n} 个目标")
    return 0


# ---------------------------------------------------------------- GUI

def gui() -> int:
    import tkinter as tk
    from tkinter import messagebox

    ui = ("Microsoft YaHei UI", 10)
    ui_b = ("Microsoft YaHei UI", 11, "bold")
    title_f = ("Microsoft YaHei UI", 16, "bold")

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("760x560")
    root.minsize(680, 480)
    root.configure(bg="#f6f7fb")

    # 顶部
    head = tk.Frame(root, bg="#f6f7fb")
    head.pack(fill="x", padx=22, pady=(18, 6))
    tk.Label(head, text=TITLE, font=title_f, bg="#f6f7fb", fg="#1b1f2a").pack(anchor="w")
    tk.Label(head, text=SUB, font=ui, bg="#f6f7fb", fg="#6b7280").pack(anchor="w", pady=(2, 0))

    # 列表
    body = tk.Frame(root, bg="#ffffff", highlightbackground="#e5e7eb",
                    highlightthickness=1)
    body.pack(fill="both", expand=False, padx=22, pady=10)

    rows = {}
    for i, name in enumerate(ORDER):
        f = tk.Frame(body, bg="#ffffff")
        f.pack(fill="x", padx=14, pady=7)
        var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(f, text=LABELS[name], variable=var, font=ui_b,
                            bg="#ffffff", activebackground="#ffffff",
                            anchor="w", width=14)
        cb.pack(side="left")
        st = tk.Label(f, text="扫描中…", font=ui, bg="#ffffff", fg="#6b7280", anchor="w")
        st.pack(side="left", fill="x", expand=True)
        rows[name] = (var, st, cb)

    # 按钮
    bar = tk.Frame(root, bg="#f6f7fb")
    bar.pack(fill="x", padx=22, pady=(4, 8))

    def mkbtn(parent, text, cmd, primary=False):
        b = tk.Button(parent, text=text, font=ui_b, relief="flat",
                      bg="#2563eb" if primary else "#e5e7eb",
                      fg="#ffffff" if primary else "#1b1f2a",
                      activebackground="#1d4ed8" if primary else "#d1d5db",
                      activeforeground="#ffffff" if primary else "#1b1f2a",
                      padx=18, pady=7, cursor="hand2", command=cmd)
        return b

    # 日志
    log = tk.Text(root, height=9, font=("Consolas", 9), bg="#0f172a", fg="#cbd5e1",
                  relief="flat", padx=10, pady=8, wrap="word")
    log.pack(fill="both", expand=True, padx=22, pady=(0, 18))
    log.configure(state="disabled")

    def say(msg: str):
        log.configure(state="normal")
        log.insert("end", msg + "\n")
        log.see("end")
        log.configure(state="disabled")

    def scan():
        """探测全部目标。放后台，避免装了很多客户端时界面卡住。"""
        def work():
            for t in all_targets():
                try:
                    s = t.status()
                except Exception as e:                       # noqa: BLE001
                    s = f"探测失败：{e}"
                name = t.name
                root.after(0, lambda n=name, s=s: apply_status(n, s))
            root.after(0, lambda: say("扫描完成。勾选要破甲的客户端，点「开始破甲」。"))

        threading.Thread(target=work, daemon=True).start()

    def apply_status(name: str, text: str):
        var, st, cb = rows[name]
        st.configure(text=text)
        # 能注入的默认勾上；不能注入的灰掉
        usable = "未安装" not in text and "没有文件级靶点" not in text \
                 and "结构未识别" not in text and "无可用靶点" not in text
        if usable:
            var.set(True)
            st.configure(fg="#059669")
        else:
            var.set(False)
            cb.configure(state="disabled")
            st.configure(fg="#9ca3af")

    def do_apply(revert=False):
        picked = [n for n, (v, _, _) in rows.items() if v.get()]
        if not picked:
            messagebox.showinfo(TITLE, "先勾一个客户端")
            return
        action = "还原" if revert else "破甲"
        btn_a.configure(state="disabled")
        btn_b.configure(state="disabled")
        say(f"—— 开始{action}：{', '.join(LABELS[n] for n in picked)}")

        def work():
            try:
                if revert:
                    for t in resolve(picked):
                        rep = t.revert()
                        root.after(0, lambda r=rep: say("  " + r.line()))
                else:
                    persona = load_persona(None)
                    for t in resolve(picked):
                        rep = t.inject(persona)
                        root.after(0, lambda r=rep: say("  " + r.line()))
                        for s in rep.skipped[:3]:
                            root.after(0, lambda x=s: say("      · " + x))
                root.after(0, lambda: say(f"—— {action}结束，重启客户端后生效"))
            except Exception:                                # noqa: BLE001
                err = traceback.format_exc(limit=3)
                root.after(0, lambda: say(err))
            finally:
                root.after(0, lambda: (btn_a.configure(state="normal"),
                                       btn_b.configure(state="normal"),
                                       scan()))

        threading.Thread(target=work, daemon=True).start()

    def do_quit():
        root.destroy()

    btn_a = mkbtn(bar, "开始破甲", lambda: do_apply(False), primary=True)
    btn_a.pack(side="left")
    btn_b = mkbtn(bar, "还原", lambda: do_apply(True))
    btn_b.pack(side="left", padx=10)
    mkbtn(bar, "重新扫描", scan).pack(side="left")
    mkbtn(bar, "退出", do_quit).pack(side="right")

    say("正在扫描本机客户端…")
    scan()
    root.mainloop()
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    # argparse 的 usage / positional arguments / options 等文案默认是英文，
    # 在 ArgumentParser 构造之前接管 _()，之后加上的 -h 选项才会用中文。
    apply_zh()
    ap = argparse.ArgumentParser(prog="xm", description=TITLE)
    ap.add_argument("cmd", nargs="?", default="gui",
                    choices=["gui", "status", "apply", "revert"])
    ap.add_argument("-t", "--target", action="append",
                    help="codex / wb / zcode / dsh，可重复")
    ap.add_argument("-p", "--persona", help="自定义人格文件")
    ap.add_argument("--dry", action="store_true", help="只算不写")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "apply":
        return cmd_apply(a.target, a.persona, a.dry)
    if a.cmd == "revert":
        return cmd_revert(a.target)
    return gui()


if __name__ == "__main__":
    raise SystemExit(main())
