"""同一 Windows 会话只开一个控制窗口；再次启动向已有窗口发恢复事件。"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class SingleInstance:
    def __init__(self, name='Local\\SamaScreen.Dashboard'):
        self.primary = True
        self.mutex = self.event = None
        self.api = None
        if os.name != 'nt':
            return
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateEventW.restype = wintypes.HANDLE
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.SetEvent.argtypes = [wintypes.HANDLE]
        self.api.SetEvent.restype = wintypes.BOOL
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        try:
            # 自动复位事件保留启动竞态中的恢复请求，不需要网络/文件/注册表。
            self.event = self.api.CreateEventW(None, False, False, name + '.Show')
            if not self.event: raise ctypes.WinError(ctypes.get_last_error())
            self.mutex = self.api.CreateMutexW(None, False, name + '.Mutex')
            error = ctypes.get_last_error()
            if not self.mutex: raise ctypes.WinError(error)
            self.primary = error != 183  # ERROR_ALREADY_EXISTS
            if not self.primary and not self.api.SetEvent(self.event):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def take_show_request(self):
        if self.event is None: return False
        result = self.api.WaitForSingleObject(self.event, 0)
        if result == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        if self.api:
            for name in ('mutex', 'event'):
                handle = getattr(self, name)
                if handle:
                    self.api.CloseHandle(handle)
                    setattr(self, name, None)
