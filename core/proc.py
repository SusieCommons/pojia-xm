"""进程枚举。只用来判断「客户端开着没」，不读内存、不注入。

CreateToolhelp32Snapshot 是纯查询接口，枚举一次全表大约 1~3 ms
（本机 200 个进程量级），比 tasklist 子进程快两个数量级。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def processes() -> set[str]:
    """当前所有进程的可执行文件名（小写）。拿不到就返回空集，不抛。"""
    names: set[str] = set()
    try:
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]

        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            return names
        try:
            e = PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            if not k.Process32FirstW(snap, ctypes.byref(e)):
                return names
            while True:
                names.add(e.szExeFile.lower())
                if not k.Process32NextW(snap, ctypes.byref(e)):
                    break
        finally:
            k.CloseHandle(snap)
    except Exception:                                          # noqa: BLE001
        return names
    return names


def any_running(wanted) -> bool:
    have = processes()
    return any(w.lower() in have for w in wanted)
