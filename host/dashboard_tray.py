"""Windows 托盘宿主；回调只投递事件，Tk 控件始终由主线程操作。"""
from __future__ import annotations

import threading
import logging

from PIL import Image, ImageDraw


class TrayHost:
    def __init__(self, events):
        self.events = events
        self.icon = None
        self.thread = None
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.failed = threading.Event()
        self._error_lock = threading.Lock()
        self._reported_errors = set()
        self.running = False

    def _error(self, operation, detail):
        # Tk receives one bounded diagnostic per operation, not one per poll.
        # Keep ready intact: a failed menu update must not prevent icon.stop.
        self.failed.set()
        with self._error_lock:
            if operation in self._reported_errors:
                return
            self._reported_errors.add(operation)
        message = f'{operation}：{detail}'[:1024]
        logging.getLogger(__name__).error('系统托盘异常：%s', message)
        self.events.put(('tray_error', message))

    def _stop_icon(self):
        try:
            self.icon.stop()
        except Exception as exc:
            self._error('stop', str(exc))

    def post(self, action):
        if not self.stopping.is_set():
            self.events.put(('tray_action', action))

    def start(self):
        if self.thread is not None:
            return
        # 延迟导入，使源码模式缺少可选依赖时窗口仍然可用。
        import pystray
        from pystray._util import win32

        class WindowIcon(pystray.Icon):
            def _create_window(self, atom):
                # pystray 0.19.5 默认放宽跨权限窗口消息过滤，本机
                # 打包EXE在这个可选步骤返回ACCESS_DENIED。程序不
                # 提权，因此只建普通顶层窗口，不修改消息过滤器。
                return win32.CreateWindowEx(0, atom, None, win32.WS_POPUP,
                    0, 0, 0, 0, 0, None, win32.GetModuleHandle(None), None)
        image = Image.new('RGB', (64, 64), '#101827')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((12, 6, 52, 58), radius=7, fill='#0891b2')
        draw.rectangle((19, 14, 45, 45), fill='#dbeafe')
        draw.rectangle((24, 37, 29, 43), fill='#0891b2')
        draw.rectangle((35, 25, 40, 43), fill='#0891b2')
        draw.ellipse((29, 49, 35, 55), fill='#101827')

        def action(name):
            return lambda icon, item: self.post(name)
        self.icon = WindowIcon('SamaScreen', image, '先马副屏 · 已停止', pystray.Menu(
            pystray.MenuItem('打开控制窗口', action('show'), default=True),
            pystray.MenuItem('启动 / 恢复显示', action('start'), enabled=lambda item: not self.running),
            pystray.MenuItem('停止显示', action('stop'), enabled=lambda item: self.running),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出程序', action('quit'))))

        def setup(icon):
            try:
                if self.stopping.is_set():
                    self._stop_icon()
                else:
                    icon.visible = True
                    self.ready.set()
                    self.events.put(('tray_ready', None))
                    if self.stopping.is_set():
                        self._stop_icon()
            except Exception as exc:
                self._error('setup', str(exc))
                self._stop_icon()

        def run():
            try:
                # Windows 后端支持专用消息线程，不占用 Tk 消息循环。
                self.icon.run(setup=setup)
            except Exception as exc:
                self._error('run', str(exc))
            finally:
                self.ready.clear()
                if not self.stopping.is_set():
                    self._error('run', '托盘线程已退出')

        self.thread = threading.Thread(target=run, name='sama-tray', daemon=True)
        self.thread.start()

    def update(self, running, text):
        self.running = running
        if self.ready.is_set() and not self.stopping.is_set() and not self.failed.is_set():
            try:
                self.icon.title = ('先马副屏 · ' + text)[:100]
                self.icon.update_menu()
            except Exception as exc:
                self._error('update', str(exc))

    def stop(self):
        self.stopping.set()
        if self.icon is not None and self.ready.is_set():
            self._stop_icon()
        return self.thread is None or not self.thread.is_alive()
