"""Windows 自定义副屏：实时面板、图片与单帧发送。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from PIL import Image, ImageTk

import dlog
from dashboard_backend import DEFAULT_OUTPUT, discover, send_once
from dashboard_render import desktop_card, fit_picture, onboard_card, test_card, live_dashboard
from dashboard_live import LiveScreen, load_settings, save_settings, sample_metrics
from dashboard_media import MediaPlayer
from desktop_session import DesktopSession
from dashboard_tray import TrayHost
from dashboard_instance import SingleInstance

TITLE = '先马副屏 · 自定义显示'


def _record_onboard_result(flow, result):
    result = dict(result)
    result['operation_id'] = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns())
    flow.DATA.mkdir(parents=True, exist_ok=True)
    history = flow.DATA / 'history'
    history.mkdir(exist_ok=True)
    payload = json.dumps(result, ensure_ascii=True, indent=2)
    (flow.DATA / 'last-result.json').write_text(payload, encoding='utf-8')
    (history / (result['operation_id'] + '.json')).write_text(payload, encoding='utf-8')
    return result


class DashboardApp:
    def __init__(self, root: tk.Tk, *, mock=False, instance=None):
        self.root = root
        self.mock = mock
        self.instance = instance
        self.instance_poll_failed = False
        self.events = queue.Queue()
        self.worker = None
        self.single_send = None
        self.closing = False
        self.tray = None
        self.hide_pending = False
        self.live = None
        self.media = None
        self.media_preview_player = None
        self.media_stop_pending = False
        self.media_next = None
        self.start_pending = False
        self.stop_pending = False
        self.settings = load_settings()
        self.media_path = self.settings.get('media', '')
        self.picture_path = self.settings['picture']
        saved_pictures = self.settings.get('pictures') or ([self.picture_path] if self.picture_path else [])
        self.picture_paths = list(dict.fromkeys(saved_pictures))
        self.picture_index = 0
        self.picture_image = None
        for index, path in enumerate(self.picture_paths):
            try:
                self.picture_image = fit_picture(path)
            except Exception:
                continue
            self.picture_index = index
            self.picture_path = path
            break
        if self.picture_image is None:
            self.picture_path = ''
        if self.settings['mode'] == '图片' and self.picture_image is None:
            self.settings['mode'] = '实时面板'
        self.latest_evidence = DEFAULT_OUTPUT
        self.image = test_card()
        self.content_mode = '测试画面'
        self.port_names = []
        root.title(TITLE + (' · 模拟模式' if mock else ''))
        root.geometry('1100x1020')
        root.minsize(1040, 1000)
        root.configure(bg='#101827')
        root.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 11))
        style.configure('TFrame', background='#101827')
        style.configure('TLabel', background='#101827', foreground='#dbeafe')
        style.configure('Muted.TLabel', foreground='#94a3b8')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold'), foreground='white')
        style.configure('Section.TLabel', font=('Microsoft YaHei UI', 15, 'bold'), foreground='white')
        style.configure('TButton', padding=(14, 10))
        style.configure('Send.TButton', background='#0891b2', foreground='white', font=('Microsoft YaHei UI', 12, 'bold'))
        outer = ttk.Frame(root, padding=22)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text='SAMA USB 副屏控制器', style='Title.TLabel').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 16))
        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky='n', padx=(0, 24))
        self.preview = ttk.Label(left)
        self.preview.pack()
        ttk.Label(left, text='画面预览 · 720 × 1568', style='Muted.TLabel').pack(pady=8)

        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky='nsew')
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        device = ttk.Frame(right)
        device.grid(row=0, column=0, sticky='ew', pady=(0, 12))
        ttk.Label(device, text='设备连接', style='Section.TLabel').pack(anchor='w')
        self.device_text = tk.StringVar(value='正在识别副屏…')
        ttk.Label(device, textvariable=self.device_text, wraplength=590, style='Muted.TLabel').pack(anchor='w', pady=(6, 8))
        device_row = ttk.Frame(device)
        device_row.pack(fill='x')
        self.port = tk.StringVar()
        self.port_box = ttk.Combobox(device_row, textvariable=self.port, state='readonly')
        self.port_box.pack(side='left', fill='x', expand=True)
        self.scan_button = ttk.Button(device_row, text='重新识别', command=self.scan)
        self.scan_button.pack(side='left', padx=(8, 0))

        if self.settings['mode'] in ('图片', 'GIF / 视频'):
            self.settings['mode'] = '板载媒体'
        self.mode = tk.StringVar(value=self.settings['mode'])
        self.notebook = ttk.Notebook(right)
        self.monitor_tab = ttk.Frame(self.notebook, padding=18)
        self.media_tab = ttk.Frame(self.notebook, padding=18)
        self.desktop_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.monitor_tab, text='  监控面板  ')
        self.notebook.add(self.media_tab, text='  板载媒体  ')
        self.notebook.add(self.desktop_tab, text='  Windows 桌面  ')
        self.notebook.grid(row=1, column=0, sticky='nsew')

        ttk.Label(self.monitor_tab, text='实时监控', style='Section.TLabel').pack(anchor='w')
        ttk.Label(self.monitor_tab, text='时间、CPU、内存、系统盘和网络上下行速率。',
                  style='Muted.TLabel', wraplength=560).pack(anchor='w', pady=(8, 18))
        monitor_options = ttk.Frame(self.monitor_tab)
        monitor_options.pack(anchor='w', fill='x')
        ttk.Label(monitor_options, text='配色').pack(side='left')
        self.theme = tk.StringVar(value=self.settings['theme'])
        self.theme_box = ttk.Combobox(monitor_options, textvariable=self.theme, state='readonly',
                                      values=['青蓝', '琥珀'], width=8)
        self.theme_box.pack(side='left', padx=(8, 18))
        self.theme_box.bind('<<ComboboxSelected>>', self.change_content)
        self.test_button = ttk.Button(monitor_options, text='生成测试画面', command=self.generate)
        self.test_button.pack(side='left')

        ttk.Label(self.media_tab, text='板载图片 / 视频', style='Section.TLabel').pack(anchor='w')
        ttk.Label(self.media_tab, text='写入副屏存储，由 T113 自己解码；不是电脑持续传输视频帧。',
                  style='Muted.TLabel', wraplength=560).pack(anchor='w', pady=(8, 12))
        onboard_kind = ttk.Frame(self.media_tab)
        onboard_kind.pack(anchor='w', fill='x', pady=(0, 8))
        self.onboard_kind = tk.StringVar(value=self.settings['onboard_kind'])
        ttk.Radiobutton(onboard_kind, text='视频循环', value='video', variable=self.onboard_kind,
                        command=self._onboard_kind_changed).pack(side='left')
        ttk.Radiobutton(onboard_kind, text='板载图片', value='image', variable=self.onboard_kind,
                        command=self._onboard_kind_changed).pack(side='left', padx=16)
        onboard_file = ttk.Frame(self.media_tab)
        onboard_file.pack(anchor='w', fill='x', pady=(0, 8))
        initial_onboard_path = self.settings['picture'] if self.onboard_kind.get() == 'image' else self.settings['media']
        self.onboard_path = tk.StringVar(value=initial_onboard_path)
        ttk.Entry(onboard_file, textvariable=self.onboard_path).pack(side='left', fill='x', expand=True)
        self.onboard_pick_button = ttk.Button(onboard_file, text='选择素材…', command=self.pick_onboard)
        self.onboard_pick_button.pack(side='left', padx=(8, 0))
        onboard_fit = ttk.Frame(self.media_tab)
        onboard_fit.pack(anchor='w', fill='x', pady=(0, 8))
        ttk.Label(onboard_fit, text='比例：').pack(side='left')
        self.onboard_fit = tk.StringVar(value=self.settings['onboard_fit'])
        for text, value in (('模糊填充', 'blur'), ('黑边', 'pad'), ('裁切填满', 'fill')):
            ttk.Radiobutton(onboard_fit, text=text, value=value,
                            variable=self.onboard_fit).pack(side='left', padx=(0, 8))
        ttk.Label(onboard_fit, text='目标').pack(side='left')
        self.onboard_target = tk.StringVar(value=str(self.settings['onboard_target']))
        self.onboard_target_box = ttk.Combobox(onboard_fit, textvariable=self.onboard_target,
            values=['16', '32', '48', '60'], width=4, state='readonly')
        self.onboard_target_box.pack(side='left', padx=(4, 0))
        ttk.Label(onboard_fit, text='MiB').pack(side='left', padx=(4, 0))
        self.onboard_rotate = tk.BooleanVar(value=self.settings['onboard_rotate'])
        self.onboard_rotate_box = ttk.Checkbutton(self.media_tab,
            text='横屏素材顺时针旋转 90°', variable=self.onboard_rotate)
        self.onboard_rotate_box.pack(anchor='w')
        self.onboard_show = tk.BooleanVar(value=self.settings['onboard_show'])
        self.onboard_show_box = ttk.Checkbutton(self.media_tab,
            text='图片上传后立即调用板端显示器', variable=self.onboard_show)
        self.onboard_show_box.pack(anchor='w')
        self.onboard_reboot = tk.BooleanVar(value=self.settings['onboard_reboot'])
        self.onboard_reboot_box = ttk.Checkbutton(self.media_tab,
            text='视频上传后请求副屏重启播放（也可手动断电）', variable=self.onboard_reboot)
        self.onboard_reboot_box.pack(anchor='w')
        ttk.Label(self.media_tab, text='固定当前素材槽＋一个上一版备份；校验通过才激活。',
                  style='Muted.TLabel', wraplength=560).pack(anchor='w', pady=(10, 0))
        recovery = ttk.Frame(self.media_tab)
        recovery.pack(anchor='w', fill='x', pady=(8, 0))
        self.onboard_restart_button = ttk.Button(recovery, text='重启当前配置',
            command=lambda: self.onboard_recovery('restart'))
        self.onboard_restart_button.pack(side='left')
        self.onboard_rollback_button = ttk.Button(recovery, text='恢复上一版并重启',
            command=lambda: self.onboard_recovery('rollback'))
        self.onboard_rollback_button.pack(side='left', padx=(8, 0))

        ttk.Label(self.desktop_tab, text='Windows 扩展桌面', style='Section.TLabel').pack(anchor='w')
        ttk.Label(self.desktop_tab, text='将虚拟显示器内容发送到实体副屏。直接读取需要权限；代理需服务已运行。',
                  style='Muted.TLabel', wraplength=560).pack(anchor='w', pady=(8, 14))
        desktop_source = ttk.Frame(self.desktop_tab)
        desktop_source.pack(anchor='w', fill='x')
        ttk.Label(desktop_source, text='源帧率').pack(side='left')
        self.desktop_fps = tk.StringVar(value=str(self.settings['desktop_fps']))
        self.desktop_fps_box = ttk.Combobox(desktop_source, textvariable=self.desktop_fps,
            values=['4', '10', '15', '30'], width=5, state='readonly')
        self.desktop_fps_box.pack(side='left', padx=(8, 18))
        self.desktop_broker = tk.BooleanVar(value=self.settings['desktop_broker'])
        self.desktop_broker_box = ttk.Checkbutton(desktop_source, text='普通用户帧代理',
            variable=self.desktop_broker)
        self.desktop_broker_box.pack(side='left')
        self.desktop_differ = tk.BooleanVar(value=self.settings['desktop_differ'])
        self.desktop_differ_box = ttk.Checkbutton(self.desktop_tab, text='启用差分 32/32（实验，有损）',
            variable=self.desktop_differ)
        self.desktop_differ_box.pack(anchor='w', pady=(12, 0))
        self.notebook.bind('<<NotebookTabChanged>>', self._on_tab_selected)

        refresh = ttk.Frame(right)
        refresh.grid(row=2, column=0, sticky='ew', pady=(10, 6))
        ttk.Label(refresh, text='刷新 / 图片轮播间隔（秒）').pack(side='left')
        self.interval = tk.StringVar(value=str(self.settings['interval']))
        self.interval_box = ttk.Combobox(refresh, textvariable=self.interval,
            values=['0.5', '1', '2', '5', '10'], width=6)
        self.interval_box.pack(side='left', padx=8)
        ttk.Button(refresh, text='保存设置', command=self.persist).pack(side='left')
        self.content_text = tk.StringVar()
        ttk.Label(right, textvariable=self.content_text, wraplength=590,
                  style='Muted.TLabel').grid(row=3, column=0, sticky='ew', pady=(2, 8))

        live_actions = ttk.Frame(right)
        live_actions.grid(row=4, column=0, sticky='ew', pady=(0, 8))
        self.start_button = ttk.Button(live_actions, text='启动 / 恢复显示', command=self.start_live, style='Send.TButton')
        self.start_button.pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(live_actions, text='停止', command=self.stop_live, state='disabled')
        self.stop_button.pack(side='left', padx=(8, 0))
        self.send_button = ttk.Button(right, text='仅发送当前一帧（不连续播放）' if not mock else '模拟发送一帧（不连接设备）',
                                      style='Send.TButton', command=self.send)
        self.send_button.grid(row=5, column=0, sticky='ew')
        self.progress = ttk.Progressbar(right, maximum=100)
        self.progress.grid(row=6, column=0, sticky='ew', pady=(8, 6))
        self.status = tk.StringVar(value='选择上方功能页签，然后点击启动；也可单次发送当前画面。')
        ttk.Label(right, textvariable=self.status, wraplength=590).grid(row=7, column=0, sticky='ew', pady=(2, 8))
        resident = ttk.Frame(right)
        resident.grid(row=8, column=0, sticky='ew')
        ttk.Button(resident, text='运行记录', command=self.open_evidence).pack(side='left')
        ttk.Button(resident, text='收起到托盘', command=self.hide_to_tray).pack(side='left', padx=8)
        ttk.Button(resident, text='退出程序', command=self.close).pack(side='left')
        ttk.Label(outer, text='停止或退出会完成当前整帧并释放串口。',
                  style='Muted.TLabel').grid(row=2, column=0, columnspan=2, sticky='w', pady=(10, 0))
        sample_metrics()
        initial_tab = self.desktop_tab if self.mode.get() == 'Windows 桌面' else (
            self.media_tab if self.mode.get() == '板载媒体' else self.monitor_tab)
        self.notebook.select(initial_tab)
        self.change_content()
        self.scan()
        root.after(100, self.poll)
        root.after(1000, self.tick)

    def _on_tab_selected(self, event=None):
        selected = self.notebook.select()
        if isinstance(self.live, DesktopSession) and selected != str(self.desktop_tab):
            self.notebook.select(self.desktop_tab)
            self.status.set('Windows 桌面仍在运行；请先停止，再切换功能。')
            return
        if selected == str(self.desktop_tab):
            mode = 'Windows 桌面'
        elif selected == str(self.media_tab):
            mode = '板载媒体'
        else:
            mode = '测试画面' if self.mode.get() == '测试画面' else '实时面板'
        if mode == '板载媒体' and self.live is not None:
            self.notebook.select(self.desktop_tab if isinstance(self.live, DesktopSession) else self.monitor_tab)
            self.status.set('实时显示仍在运行；请先停止，再上传板载素材。')
            return
        self.mode.set(mode)
        self.change_content()

    def _onboard_kind_changed(self):
        self.onboard_path.set(self.picture_path if self.onboard_kind.get() == 'image' else self.media_path)
        self.change_content()

    def pick_onboard(self):
        image = self.onboard_kind.get() == 'image'
        name = filedialog.askopenfilename(title='选择板载图片' if image else '选择板载循环视频',
            filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp *.webp')] if image else
                      [('视频', '*.mp4 *.mkv *.avi *.mov *.webm'), ('所有文件', '*.*')])
        if name:
            self.onboard_path.set(str(Path(name).resolve()))
            self.change_content()

    def start_onboard(self):
        if self.mock:
            self.status.set('Mock 模式不会上传素材或修改副屏配置。')
            return False
        if self.live or self.closing or (self.worker and self.worker.is_alive()):
            return False
        source = Path(self.onboard_path.get().strip())
        if not source.is_file():
            self.status.set('请先选择存在的图片或视频素材。')
            return False
        image = self.onboard_kind.get() == 'image'
        if image:
            self.picture_path = str(source.resolve())
        else:
            self.media_path = str(source.resolve())
        fit = self.onboard_fit.get()
        rotate = self.onboard_rotate.get()
        show = self.onboard_show.get()
        reboot = self.onboard_reboot.get()
        try:
            target_mib = int(self.onboard_target.get())
            if not 1 <= target_mib <= 63:
                raise ValueError
        except ValueError:
            self.status.set('目标大小须为 1–63 MiB。')
            return False
        target_bytes = target_mib * 1024 * 1024
        port_name = self.port.get() or None
        self.set_busy(True)
        self.stop_button.configure(state='disabled')
        self.progress['value'] = 0
        self.status.set('正在准备板载素材；上传过程中不会自动重试。')

        progress_state = {'stage': 'prepare', 'value': 0.0}

        def report(stage, value):
            progress_state.update(stage=stage, value=value)
            prepare_share = 0.2 if image else 0.7
            ranges = {'prepare': (0.0, prepare_share), 'backup': (prepare_share, prepare_share),
                      'upload': (prepare_share, 0.88), 'verify': (0.88, 0.90),
                      'stage': (0.90, 0.93), 'commit': (0.93, 0.96),
                      'activate': (0.96, 1.0)}
            start, end = ranges[stage]
            overall = start + value * (end - start)
            self.events.put(('onboard_progress', (stage, max(0, min(1, overall)), value)))

        def task():
            completed = {}
            try:
                import tempfile
                import onboard_flow as flow
                import onboard_media as media
                flow.DATA.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='prepare-', dir=flow.DATA) as folder:
                    with flow.session_scope(port_name):
                        if image:
                            prepared = media.prepare_image(source, Path(folder), fit=fit, rotate=rotate)
                            report('prepare', 1)
                            report('upload', 0)
                            sent = flow.push_image(prepared['path'],
                                progress=lambda value: report('upload', value))
                            completed['upload'] = sent
                            if not sent.get('ok') or not sent['copied_and_verified']:
                                raise RuntimeError('图片上传或设备端校验失败；未切换配置')
                            report('activate', 0)
                            activated = flow.set_mode(1)
                            completed['config'] = activated
                            if not activated['confirmed']:
                                raise RuntimeError('图片模式写入后读回不一致')
                            shown = flow.show_image(sent['device_path']) if show else None
                            result = dict(ok=True, kind='image', device_path=sent['device_path'],
                                md5=sent['md5'], activated=True, upload=sent, config=activated,
                                shown=shown['shown'] if shown is not None else None)
                        else:
                            prepared = media.prepare(source, Path(folder), fit=fit, rotate=rotate,
                                progress=lambda value: report('prepare', value), target_bytes=target_bytes)
                            report('upload', 0)
                            sent = flow.push_video(prepared['path'],
                                progress=lambda value: report('upload', value),
                                step=lambda stage: report(stage, 0))
                            completed['upload'] = sent
                            if not (sent['verified_on_tmp'] and sent['staged_and_verified']
                                    and sent['copied_and_verified']):
                                raise RuntimeError('视频上传、落盘或设备端校验失败；未切换配置')
                            report('activate', 0)
                            activated = flow.apply_config(sent['device_path'])
                            completed['config'] = activated
                            if not activated['written']:
                                raise RuntimeError('视频配置写入或落盘校验未确认')
                            if reboot:
                                flow.reboot_device()
                            result = dict(ok=True, kind='video', device_path=sent['device_path'],
                                md5=sent['md5'], activated=True, reboot_requested=reboot,
                                upload=sent, config=activated,
                                note='板端即将重启并循环播放' if reboot else '断电重启后由板端循环播放')
                _record_onboard_result(flow, result)
                self.events.put(('onboard_done', result))
            except Exception as exc:
                detail = str(exc)
                if 'no reply at all' in detail:
                    detail = '副屏命令通道无响应。请拔插副屏 USB 后再决定是否重试；本次未自动重试。'
                failure = dict(ok=False, error=detail, stage=progress_state['stage'],
                               stage_progress=progress_state['value'], **completed)
                try:
                    if 'flow' in locals():
                        _record_onboard_result(flow, failure)
                    else:
                        output = DEFAULT_OUTPUT.parent / 'onboard'
                        output.mkdir(parents=True, exist_ok=True)
                        (output / 'last-result.json').write_text(
                            json.dumps(failure, ensure_ascii=True, indent=2), encoding='utf-8')
                except OSError as record_error:
                    failure['record_error'] = str(record_error)
                self.events.put(('onboard_error', failure))

        try:
            worker = threading.Thread(target=task, name='sama-onboard', daemon=False)
            worker.start()
        except Exception as exc:
            self.worker = None
            self.set_busy(False)
            self.status.set(f'板载任务启动失败：{exc}')
            return False
        self.worker = worker
        return True

    def onboard_recovery(self, action):
        if self.mock:
            self.status.set('Mock 模式不会重启副屏或恢复素材。')
            return False
        if self.live or self.closing or (self.worker and self.worker.is_alive()):
            return False
        if action not in ('restart', 'rollback'):
            raise ValueError('unknown recovery action')
        port_name = self.port.get() or None
        self.set_busy(True)
        self.progress['value'] = 0
        self.status.set('正在执行板载恢复操作…')

        def task():
            import onboard_flow as flow
            try:
                with flow.session_scope(port_name):
                    if action == 'rollback':
                        restored = flow.rollback_video()
                        if not restored.get('rolled_back'):
                            raise RuntimeError('没有可恢复的上一版视频')
                    flow.reboot_device()
                result = dict(ok=True, kind='recovery', action=action,
                              note='已请求副屏重启；请观察当前配置或上一版视频')
                _record_onboard_result(flow, result)
                self.events.put(('onboard_done', result))
            except Exception as exc:
                failure = dict(ok=False, error=str(exc), stage='recovery', stage_progress=0)
                _record_onboard_result(flow, failure)
                self.events.put(('onboard_error', failure))

        try:
            worker = threading.Thread(target=task, name='sama-onboard-recovery', daemon=False)
            worker.start()
        except Exception as exc:
            self.worker = None
            self.set_busy(False)
            self.status.set(f'恢复任务启动失败：{exc}')
            return False
        self.worker = worker
        return True

    def update_preview(self):
        self.preview_image = ImageTk.PhotoImage(self.image.resize((252, 549), Image.Resampling.LANCZOS))
        self.preview.configure(image=self.preview_image)

    def generate(self):
        self.mode.set('测试画面')
        self.change_content()

    def change_content(self, event=None):
        if isinstance(self.live, DesktopSession) and self.mode.get() != 'Windows 桌面':
            self.mode.set('Windows 桌面')
            self.status.set('Windows 桌面仍在运行；请先停止，再切换其他内容。')
            return False
        if self.mode.get() != '板载媒体':
            self.start_button.configure(text='启动 / 恢复显示')
        # 先选文件再停旧内容。对话框取消时保持已生效模式，
        # 同时避免对话框的嵌套Tk循环刷新到尚未选好文件的模式。
        if self.mode.get() == '图片' and self.picture_image is None:
            self.mode.set(self.content_mode)
            return self.open_picture()
        if self.mode.get() == 'GIF / 视频' and not self.media_path:
            self.mode.set(self.content_mode)
            return self.open_media()
        if self.mode.get() != 'GIF / 视频':
            if self.start_pending:
                self.start_pending = False
                self.set_busy(False)
                self.stop_button.configure(state='disabled')
            if not self.stop_media():
                self.media_next = self.change_content
                return False
        if self.mode.get() == '实时面板':
            self.image = live_dashboard(sample_metrics(), self.theme.get())
            self.content_text.set('实时时间、CPU、内存、系统盘和网络速率')
        elif self.mode.get() == '图片' and self.picture_image is not None:
            self.image = self.picture_image.copy()
            count = len(self.picture_paths)
            prefix = '图片轮播' if count > 1 else '图片'
            self.content_text.set(f'{prefix}：{Path(self.picture_path).name} · {self.picture_index + 1}/{count}')
        elif self.mode.get() == 'GIF / 视频':
            if self.media and (self.media.error or self.media.halt.is_set()):
                if not self.stop_media():
                    self.media_next = self.change_content
                    return False
            if self.media is None:
                try:
                    player = MediaPlayer(self.media_path, rotate_landscape=True,
                        notify=lambda k, v: self.events.put(('media_event', (player, k, v))))
                    self.media = player
                    player.start()
                except Exception as exc:
                    self.stop_media()
                    self.status.set(f'媒体无法打开：{exc}')
                    return False
            self.content_text.set(f'循环播放：{Path(self.media_path).name} · 电脑解码 · 无声音')
            self.content_mode = self.mode.get()
            return True
        elif self.mode.get() == '板载媒体':
            self.image = onboard_card()
            self.content_text.set('板载媒体：写入副屏存储，由 T113 自己解码；固定槽位并保留上一版')
            self.start_button.configure(text='上传并激活板载媒体')
            self.send_button.configure(state='disabled')
        elif self.mode.get() == 'Windows 桌面':
            self.image = desktop_card()
            source = '普通用户帧代理' if self.desktop_broker.get() else '直接读取虚拟显示器'
            differ = '差分 32/32' if self.desktop_differ.get() else '整帧 cmd200'
            self.content_text.set(f'Windows 扩展桌面 · {source} · 源 {self.desktop_fps.get()} fps · {differ}')
        else:
            self.image = test_card()
            self.content_text.set('测试画面：时间、红绿蓝色块、棋盘格和方向标记')
        self.content_mode = self.mode.get()
        self.update_preview()
        if self._live_accepts_frames():
            self.live.submit(self.image.copy())
        return True

    def interval_seconds(self):
        value = float(self.interval.get())
        if not 0.5 <= value <= 60:
            raise ValueError('刷新间隔须在 0.5 到 60 秒之间')
        return value

    def _advance_picture(self):
        count = len(self.picture_paths)
        if self.mode.get() != '图片' or count < 2:
            return False
        for step in range(1, count + 1):
            index = (self.picture_index + step) % count
            path = self.picture_paths[index]
            try:
                image = fit_picture(path)
            except Exception:
                continue
            self.picture_index = index
            self.picture_path = path
            self.picture_image = image
            self.image = image.copy()
            self.content_text.set(f'图片轮播：{Path(path).name} · {index + 1}/{count} · 循环')
            self.update_preview()
            if self._live_accepts_frames():
                self.live.submit(self.image.copy())
            return True
        self.status.set('轮播列表中的图片当前都无法打开；保留最后有效画面。')
        return False

    def persist(self):
        try:
            save_settings({'interval': self.interval_seconds(), 'theme': self.theme.get(),
                           'mode': self.mode.get(), 'picture': self.picture_path,
                           'media': self.media_path, 'pictures': self.picture_paths,
                           'desktop_broker': self.desktop_broker.get(),
                           'desktop_differ': self.desktop_differ.get(),
                           'desktop_fps': self.desktop_fps.get(),
                           'onboard_kind': self.onboard_kind.get(),
                           'onboard_fit': self.onboard_fit.get(),
                           'onboard_rotate': self.onboard_rotate.get(),
                           'onboard_reboot': self.onboard_reboot.get(),
                           'onboard_show': self.onboard_show.get(),
                           'onboard_target': self.onboard_target.get()})
            self.status.set('设置已保存。下次启动会恢复；点击启动后才发送。')
            return True
        except (OSError, ValueError) as exc:
            self.status.set(f'设置未保存：{exc}')
            return False

    def tick(self):
        if self.closing:
            # A failed asynchronous stop can cancel close. Keep the single
            # refresh chain alive until Tk actually exits, without submitting.
            self.root.after(1000, self.tick)
            return
        if self.media_stop_pending:
            # poll负责完成用户排队的换片；自动刷新不能覆盖media_next。
            pass
        elif self.mode.get() == '实时面板':
            self.change_content()
        elif self.mode.get() == '图片' and len(self.picture_paths) > 1:
            self._advance_picture()
        elif self.mode.get() not in ('GIF / 视频', 'Windows 桌面') and self._live_accepts_frames():
            self.live.submit(self.image.copy())
        try:
            delay = int(self.interval_seconds() * 1000)
        except ValueError:
            delay = 1000
        self.root.after(delay, self.tick)

    def start_live(self):
        if self.live or self.start_pending or getattr(self, 'single_send', None) is not None or (self.worker and self.worker.is_alive()) or self.closing:
            return
        if self.media_stop_pending:
            self.status.set('正在切换画面，请稍后启动显示。')
            return
        if not self.persist():
            return
        if self.mode.get() == '板载媒体':
            return self.start_onboard()
        if self.mode.get() == 'Windows 桌面':
            return self._start_desktop_ready()
        if self.mode.get() == 'GIF / 视频':
            if not self.change_content():
                if self.media_stop_pending:
                    self.media_next = self.start_live
                return
            self._take_media_frame()
            if not self._media_frame_ready():
                if self.media and (self.media.error is not None or self.media.halt.is_set()):
                    self._reject_media_start()
                    return
                self.start_pending = True
                self.start_button.configure(state='disabled')
                self.send_button.configure(state='disabled')
                self.stop_button.configure(state='normal')
                self.status.set('正在准备媒体首帧，可点击停止取消…')
                return
        self._start_live_ready()

    def _media_frame_ready(self):
        player = self.media
        return bool(self.mode.get() == 'GIF / 视频' and not self.media_stop_pending
                    and player is not None and player.error is None
                    and not player.halt.is_set() and self.media_preview_player is player)

    def _take_media_frame(self):
        # decoded counts are not display-ready frames: a decoder may fail after
        # publishing, or a replacement may not yet have updated the old preview.
        player = self.media
        if (self.mode.get() != 'GIF / 视频' or self.media_stop_pending or player is None
                or player.error is not None or player.halt.is_set()):
            return None
        frame = player.take()
        if (frame is None or player is not self.media or player.error is not None
                or player.halt.is_set()):
            return None
        self.media_preview_player = None
        self.image = frame
        self.update_preview()
        if player is not self.media or player.error is not None or player.halt.is_set():
            return None
        self.media_preview_player = player
        return frame

    def _reject_media_start(self):
        self.start_pending = False
        self.set_busy(False)
        self.stop_button.configure(state='disabled')
        detail = self.media.error if self.media and self.media.error is not None else '媒体未就绪或已停止'
        self.status.set(f'媒体无法启动：{detail}；未创建显示会话。')

    def _start_desktop_ready(self):
        driver = None
        try:
            fps = int(self.desktop_fps.get())
            if not 1 <= fps <= 30:
                raise ValueError('桌面源帧率须为 1–30')
            driver = DesktopSession(mock=self.mock, preferred_port=self.port.get() or None,
                broker=self.desktop_broker.get(), differ=self.desktop_differ.get(),
                source_fps=fps,
                notify=lambda kind, value: self.events.put(('live', (kind, value))))
            driver.start()
        except Exception as exc:
            released = True
            if driver is not None:
                try:
                    released = driver.shutdown(wait_s=0)
                except Exception:
                    released = False
            self.live = None if released else driver
            self.stop_button.configure(state='disabled' if released else 'normal')
            self.status.set(f'Windows 桌面启动失败：{exc}')
            return False
        self.live = driver
        self.stop_pending = False
        self.latest_evidence = driver.directory
        self.start_button.configure(state='disabled')
        self.send_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.port_box.configure(state='disabled')
        self.scan_button.configure(state='disabled')
        for box in (self.desktop_fps_box, self.desktop_broker_box, self.desktop_differ_box):
            box.configure(state='disabled')
        self.status.set('Windows 桌面会话已启动，正在读取虚拟显示器并确认副屏…' if not self.mock
                        else '桌面模拟会话已启动：重放合成帧，不读取桌面或连接设备。')
        self.update_tray('桌面显示运行中')
        return True

    def _start_live_ready(self):
        if self.mode.get() == 'GIF / 视频' and not self._media_frame_ready():
            self._reject_media_start()
            return False
        self.start_pending = False
        driver = None
        try:
            # Prepare/queue the first frame before starting a thread that may
            # connect. A copy/directory/queue failure must not leave it running.
            frame = self.image.copy()
            driver = LiveScreen(mock=self.mock, preferred_port=self.port.get() or None,
                                notify=lambda kind, value: self.events.put(('live', (kind, value))))
            driver.submit(frame)
            driver.start()
        except Exception as exc:
            released = True
            cleanup_error = None
            if driver is not None:
                try:
                    released = driver.shutdown(wait_s=0)
                except Exception as cleanup:
                    released = False
                    cleanup_error = str(cleanup)
            self.live = None if released else driver
            self.set_busy(not released)
            self.stop_button.configure(state='disabled' if released else 'normal')
            detail = f'显示启动失败：{exc}'
            if not released:
                detail += '；收尾未确认，请点击停止后再试'
                if cleanup_error:
                    detail += f'（{cleanup_error}）'
            self.status.set(detail)
            self.update_tray('启动失败' if released else '启动失败，待收尾')
            return False
        self.live = driver
        self.stop_pending = False
        self.latest_evidence = self.live.directory
        self.start_button.configure(state='disabled')
        self.send_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.port_box.configure(state='disabled')
        self.scan_button.configure(state='disabled')
        self.status.set('实时显示已启动，正在确认设备身份…')
        self.update_tray('正在连接')
        return True

    def stop_live(self):
        self.media_next = None
        if self.start_pending:
            self.start_pending = False
            self.stop_media()
            self.set_busy(False)
            self.stop_button.configure(state='disabled')
            self.status.set('已取消媒体启动，未连接设备。')
            return
        if not self.live or (self.worker and self.worker.is_alive()):
            return
        self.stop_pending = True
        self.stop_media()
        self.stop_button.configure(state='disabled')
        self.status.set('正在完成当前整帧并释放端口…')
        self.update_tray('正在停止')
        driver = self.live
        def task():
            try:
                while not driver.shutdown(wait_s=0.5):
                    pass
            except Exception as exc:
                self.events.put(('live_stop_failed', (driver, str(exc))))
            else:
                self.events.put(('live_stopped', driver))
        try:
            worker = threading.Thread(target=task, name='sama-stop', daemon=False)
            worker.start()
        except Exception as exc:
            self.worker = None
            self._stop_failed(driver, str(exc))
        else:
            self.worker = worker

    def _stop_failed(self, driver, detail):
        if driver is not self.live:
            return
        self.worker = None
        self.set_busy(True)
        self.stop_button.configure(state='normal')
        if self.closing:
            self.closing = False
            self.show_window()
        self.status.set(f'停止未完成：{detail}；仍保留当前会话，请点击停止重试，端口释放未确认。')
        self.update_tray('停止未完成，请打开窗口')

    def _live_accepts_frames(self):
        # The stop button is re-enabled for retry after failure. That does not
        # mean this session may accept new content while release is unknown.
        return bool(self.live and not isinstance(self.live, DesktopSession)
                    and not self.closing and not getattr(self, 'stop_pending', False)
                    and not self.stop_button.instate(['disabled']))

    def open_picture(self):
        names = filedialog.askopenfilenames(title='选择一张或多张轮播图片', filetypes=[
            ('图片', '*.png *.jpg *.jpeg *.bmp *.webp'), ('所有文件', '*.*')])
        if not names:
            return False
        if len(names) > 100:
            self.status.set('一次最多选择 100 张图片。')
            return False
        paths = [str(Path(name).resolve()) for name in names]
        for index, path in enumerate(paths):
            try:
                image = fit_picture(path)
            except Exception:
                continue
            self.picture_paths = paths
            self.picture_index = index
            self.picture_path = path
            self.picture_image = image
            self.mode.set('图片')
            return self.change_content()
        self.status.set('所选图片都无法打开；保留原有图片和播放状态。')
        return False

    def stop_media(self):
        if self.media:
            # Tk 主线程只发停止请求，poll 回收；不能让关闭/切换卡住五秒。
            if not self.media.stop(wait_s=0):
                self.media_stop_pending = True
                self.status.set('正在等待媒体解码线程退出…')
                return False
            self.media = None
            self.media_preview_player = None
        self.media_stop_pending = False
        self.media_next = None
        return True

    def open_media(self):
        name = filedialog.askopenfilename(title='选择循环播放的 GIF 或视频', filetypes=[
            ('动图 / 视频', '*.gif *.mp4 *.mkv *.mov *.avi *.webm'), ('所有文件', '*.*')])
        if not name:
            return False
        return self._open_media_path(name)

    def _open_media_path(self, name):
        if not self.stop_media():
            self.media_next = lambda: self._open_media_path(name)
            return False
        self.media_path = str(Path(name).resolve())
        self.mode.set('GIF / 视频')
        return self.change_content()

    def scan(self):
        try:
            ports = [] if self.mock else discover()
            self.port_names = [p.device for p in ports]
            values = ['模拟设备'] if self.mock else self.port_names
            self.port_box['values'] = values
            if self.port.get() not in values:
                self.port.set(values[0] if values else '')
            self.device_text.set('模拟模式 · 不打开串口' if self.mock else
                                 ('找到图像接口：' + '、'.join(values) + ' · 1D6B:A065' if values else
                                  '未找到副屏。请检查 USB 连接，然后重新识别。'))
            self.send_button.configure(state='normal' if values and not self.live else 'disabled')
        except Exception as exc:
            self.device_text.set(f'识别失败：{exc}')
            self.send_button.configure(state='disabled')

    def set_busy(self, busy):
        for button in (self.scan_button, self.test_button, self.onboard_pick_button,
                       self.onboard_restart_button, self.onboard_rollback_button,
                       self.send_button, self.start_button):
            button.configure(state='disabled' if busy else 'normal')
        self.theme_box.configure(state='disabled' if busy else 'readonly')
        self.interval_box.configure(state='disabled' if busy else 'normal')
        for box in (self.desktop_fps_box, self.desktop_broker_box, self.desktop_differ_box,
                    self.onboard_rotate_box, self.onboard_reboot_box, self.onboard_show_box,
                    self.onboard_target_box):
            box.configure(state='disabled' if busy else
                          ('readonly' if box in (self.desktop_fps_box, self.onboard_target_box) else 'normal'))
        self.port_box.configure(state='disabled' if busy else 'readonly')

    def send(self):
        if self.live or self.closing or self.start_pending or getattr(self, 'single_send', None) is not None or (self.worker and self.worker.is_alive()):
            return
        if self.media_stop_pending:
            self.status.set('正在切换画面，请等预览更新后再发送。')
            return
        if self.mode.get() == 'GIF / 视频':
            self._take_media_frame()
            if not self._media_frame_ready():
                self.status.set('媒体未就绪或已停止，请重新选择 / 启动并等待有效预览后再发送。')
                return
        try:
            image, port = self.image.copy(), self.port.get() or None
        except Exception as exc:
            self.status.set(f'发送准备失败：{exc}；未启动发送任务。')
            return
        token = object()
        self.single_send = token
        self.set_busy(True)
        self.progress['value'] = 0
        self.status.set('正在确认设备身份并发送，请稍候…')

        def task():
            try:
                result = send_once(image, port=port, mock=self.mock,
                                   progress=lambda sent, total: self.events.put(('single_progress', (token, sent / total * 100))))
                self.events.put(('single_done', (token, result)))
            except Exception as exc:
                self.events.put(('single_error', (token, str(exc))))

        try:
            worker = threading.Thread(target=task, name='sama-single-frame', daemon=False)
            worker.start()
        except Exception as exc:
            self.single_send = None
            self.worker = None
            self.set_busy(False)
            self.status.set(f'发送启动失败：{exc}；未启动发送任务，可重试。')
        else:
            self.worker = worker

    def poll(self):
        if self.instance and not self.closing and not self.instance_poll_failed:
            try:
                requested = self.instance.take_show_request()
            except Exception as exc:
                # Keep the mutex owner alive: releasing it here could admit a
                # second controller while this session still owns the device.
                # Only disable auxiliary wake-up polling, never the GUI queue
                # that drains media events and display shutdown completions.
                self.instance_poll_failed = True
                detail = str(exc)[:1024]
                logging.getLogger(__name__).error('单实例通知异常：%s', detail)
                self.show_window()
                self.status.set(f'单实例窗口唤起不可用：{detail}；当前控制与停止仍可使用。')
            else:
                if requested:
                    self.show_window()
        if self.media and self.media_stop_pending:
            if self.media.stop(wait_s=0):
                self.media = None
                self.media_preview_player = None
                self.media_stop_pending = False
                action, self.media_next = self.media_next, None
                if action and not self.closing:
                    action()
        if self.media and not self.closing and not self.media_stop_pending:
            if self.start_pending and (self.media.error is not None or self.media.halt.is_set()):
                self._reject_media_start()
            frame = self._take_media_frame()
            if frame is not None:
                if self.start_pending:
                    self._start_live_ready()
                elif self._live_accepts_frames() and self._media_frame_ready():
                    self.live.submit(frame)
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                self.progress['value'] = value
            elif kind in ('single_progress', 'single_done', 'single_error'):
                token, detail = value
                if token is not getattr(self, 'single_send', None):
                    continue
                if kind == 'single_progress':
                    self.progress['value'] = detail
                    continue
                if self.worker and self.worker.is_alive():
                    # Publication can precede thread exit. Keep task ownership
                    # and controls locked until both are observed by Tk.
                    self.events.put((kind, value))
                    break
                self.single_send = None
                self.worker = None
                if kind == 'single_done':
                    self.latest_evidence = Path(detail['evidence_dir'])
                    self.status.set(('模拟发送完成' if self.mock else '主机发送完成，请看实体屏确认') +
                                    f"。图像传输 {detail['image_seconds']:.2f} 秒，端口已释放。")
                else:
                    self.status.set(f'发送已停止：{detail}\n若端口被占用，请先关闭原厂软件；然后重新识别。')
                self.set_busy(self.closing)
            elif kind == 'tray_ready':
                if self.hide_pending and not self.closing and self.tray and not self.tray.failed.is_set():
                    self.hide_pending = False
                    self.root.withdraw()
                    self.update_tray('显示运行中' if self.live else '已停止')
            elif kind == 'tray_error':
                logging.getLogger(__name__).error('系统托盘异常：%s', value)
                self.hide_pending = False
                if self.closing:
                    # A failed tray stop must not leave an invisible process
                    # waiting for its still-live backend thread to exit.
                    self.root.deiconify()
                    self.root.lift()
                else:
                    self.show_window()
                self.status.set(f'系统托盘不可用：{value}；请保持控制窗口打开。')
            elif kind == 'tray_action':
                if not self.closing:
                    if value == 'show': self.show_window()
                    elif value == 'start': self.start_live()
                    elif value == 'stop': self.stop_live()
                    elif value == 'quit': self.close()
            elif kind == 'media_event':
                player, event, detail = value
                if player is self.media and event == 'media_error':
                    if self.start_pending:
                        self.start_pending = False
                        self.set_busy(False)
                        self.stop_button.configure(state='disabled')
                    self.status.set(f'媒体播放已停止：{detail}')
            elif kind == 'onboard_progress':
                stage, overall, stage_value = value
                labels = {'prepare': '正在处理素材', 'backup': '正在停止播放并备份当前视频',
                          'upload': '正在上传到副屏', 'verify': '正在校验上传数据',
                          'stage': '正在写入机身暂存并落盘', 'commit': '正在原子替换并复核',
                          'activate': '正在写入并校验播放配置'}
                self.progress['value'] = overall * 100
                suffix = f' {stage_value * 100:.0f}%' if stage in ('prepare', 'upload') else ''
                self.status.set(labels[stage] + '…' + suffix)
            elif kind in ('onboard_done', 'onboard_error'):
                if self.worker and self.worker.is_alive():
                    self.events.put((kind, value))
                    break
                self.worker = None
                self.set_busy(self.closing)
                self.stop_button.configure(state='disabled')
                if kind == 'onboard_error':
                    detail = value.get('error') if isinstance(value, dict) else str(value)
                    if isinstance(value, dict) and value.get('config', {}).get('written'):
                        self.status.set('媒体与配置已完成，仅软件重启失败；请手动拔插，不要重新上传。')
                    else:
                        self.status.set(f'板载任务失败：{detail}；未自动重试。')
                else:
                    self.progress['value'] = 100
                    self.latest_evidence = DEFAULT_OUTPUT.parent / 'onboard'
                    self.status.set('板载任务完成：' + value.get('note', '图片已激活并显示'))
            elif kind == 'live':
                event, detail = value
                if event == 'connected':
                    self.device_text.set(f"已连接：{detail['port']} · {detail['identity'] or '模拟设备'}")
                    self.update_tray('显示运行中')
                elif event == 'offline':
                    self.status.set(detail)
                    self.update_tray('连接中断，等待重连')
                elif event == 'fatal':
                    self.status.set(f'显示线程已停止：{detail}；请点击停止后重新启动。')
                    self.update_tray('显示异常，请打开窗口')
                elif event == 'frame' and self._live_accepts_frames():
                    self.progress['value'] = 100
                    self.status.set(f'媒体播放已停止：{self.media.error}' if self.media and self.media.error else
                        ('模拟' if self.mock else '实时') + f"显示中 · 已发送 {detail['count']} 帧 · 传输 {detail['seconds']:.2f} 秒")
            elif kind == 'live_stopped':
                if value is not self.live:
                    continue
                if self.worker and self.worker.is_alive():
                    # Event publication precedes helper thread exit. Do not
                    # drop its last reference or destroy Tk in that interval.
                    self.events.put((kind, value))
                    break
                summary = self.live.summary if self.live else None
                self.live = None
                self.stop_pending = False
                self.worker = None
                self.set_busy(False)
                self.stop_button.configure(state='disabled')
                failure = (summary.get('fatal_error') or
                           ('会话摘要未保存' if summary.get('summary_saved') is False else None) or
                           ('端口释放未确认' if not summary.get('port_released') else None)) if summary else '未取得会话收尾摘要，端口释放未确认'
                self.status.set(f"停止异常：{failure}" if failure else
                    '已停止，端口已释放。可点击“启动 / 恢复显示”继续。')
                self.update_tray('停止异常' if failure else '已停止')
            elif kind == 'live_stop_failed':
                driver, detail = value
                if driver is not self.live:
                    continue
                if self.worker and self.worker.is_alive():
                    self.events.put((kind, value))
                    break
                self._stop_failed(driver, detail)
        if self.closing and self.media is None and self.live is None and getattr(self, 'single_send', None) is None and not (self.worker and self.worker.is_alive()):
            if self.tray is None or self.tray.stop():
                self.root.destroy()
                return
        self.root.after(100, self.poll)

    def open_evidence(self):
        self.latest_evidence.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.latest_evidence))

    def update_tray(self, text):
        if self.tray:
            self.tray.update(self.live is not None, text)

    def show_window(self):
        self.hide_pending = False
        if not self.closing:
            self.root.deiconify()
            self.root.lift()

    def hide_to_tray(self):
        if self.closing:
            return
        try:
            if self.tray and (self.tray.failed.is_set() or
                              (self.tray.thread and not self.tray.thread.is_alive())):
                self.status.set('系统托盘已停止；请保持窗口打开，退出后重新启动程序。')
                self.show_window()
                return
            if self.tray is None:
                self.tray = TrayHost(self.events)
                self.tray.start()
            if self.tray.ready.is_set():
                self.root.withdraw()
            else:
                self.hide_pending = True
        except Exception as exc:
            logging.getLogger(__name__).exception('系统托盘初始化失败')
            self.tray = None
            self.hide_pending = False
            self.status.set(f'系统托盘不可用：{exc}；请保持控制窗口打开。')

    def close(self):
        if self.closing:
            return
        self.persist()
        self.start_pending = False
        self.media_next = None
        self.stop_media()
        self.hide_pending = False
        self.closing = True
        self.set_busy(True)
        self.stop_button.configure(state='disabled')
        if self.tray:
            self.tray.stop()
        if self.live:
            self.stop_live()
            self.set_busy(True)
        elif getattr(self, 'single_send', None) is not None or (self.worker and self.worker.is_alive()):
            self.set_busy(True)
            self.status.set('正在完成本次传输并释放端口，随后退出…')
        elif self.media is None:
            if self.tray is None or self.tray.stop():
                self.root.destroy()


def _cli_media_error(player):
    """Check a started source before taking/submitting; never after our stop.

    A finished loop decoder is not a healthy source even if its latest-frame
    mailbox still contains an image. This is a host-side state check, not an
    atomic guarantee against a decoder failing immediately after submission.
    """
    if player.error is not None:
        return player.error or '媒体解码失败'
    if player.halt.is_set():
        return '媒体解码已停止'
    if not player.worker.is_alive():
        return '媒体解码线程已退出'
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='先马 6.5 英寸自定义副屏')
    parser.add_argument('--mock', action='store_true', help='模拟模式，不打开串口')
    parser.add_argument('--send-test', action='store_true', help='明确请求发送一张测试图后退出')
    parser.add_argument('--render-only', action='store_true', help='只保存测试图，不打开设备或窗口')
    parser.add_argument('--smoke-ui', action='store_true', help='只测试窗口启动，自动退出；不打开串口')
    parser.add_argument('--live-seconds', type=float, help='运行指定秒数的实时面板后释放端口退出')
    parser.add_argument('--media', help='配合 --live-seconds 循环播放 GIF / 视频；电脑解码，无声音')
    parser.add_argument('--strict-session', action='store_true', help='验收模式：首次设备错误即停止，不自动重连')
    parser.add_argument('--max-frames', type=int, help='命令行最多提交图像数，在会话期限内等待最后提交发送完成后整帧退出')
    args = parser.parse_args(argv)
    if args.media and args.live_seconds is None:
        parser.error('--media 须配合 --live-seconds 使用')
    if args.max_frames is not None and (args.live_seconds is None or args.max_frames<1):
        parser.error('--max-frames 须配合 --live-seconds 使用且至少为1')
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    dlog.setup(log_dir=str(DEFAULT_OUTPUT), quiet=True)
    if args.render_only:
        path = DEFAULT_OUTPUT / 'test-card.png'
        test_card().save(path)
        print(path)
        return 0
    if args.send_test:
        print(json.dumps(send_once(test_card(), mock=args.mock), ensure_ascii=False, indent=2))
        return 0
    if args.live_seconds is not None:
        if not 1 <= args.live_seconds <= 3610:
            parser.error('--live-seconds 须在 1 到 3610 之间')
        driver = LiveScreen(mock=args.mock, fail_on_offline=args.strict_session)
        media = MediaPlayer(args.media) if args.media else None
        last_submission = None
        last_submission_completed = None
        submitted = 0
        media_started = False
        media_error = None
        try:
            sample_metrics()
            driver.start()
            if media:
                media.start()
                media_started = True
            end = time.monotonic() + args.live_seconds
            while time.monotonic() < end:
                if args.strict_session and (driver.health.offline_events or not driver._worker.is_alive()):
                    break
                if media:
                    media_error = _cli_media_error(media)
                    if media_error is not None:
                        break
                frame = media.take() if media else live_dashboard(sample_metrics())
                if media:
                    # take() and decode run concurrently: recheck before the
                    # returned frame crosses into the display queue.
                    media_error = _cli_media_error(media)
                    if media_error is not None:
                        break
                if frame is not None:
                    last_submission = driver.submit(frame)
                    submitted += 1
                    if args.max_frames is not None and submitted>=args.max_frames:
                        break
                time.sleep(min(.05 if media else 1, max(0, end - time.monotonic())))
            if args.max_frames is not None:
                last_submission_completed = bool(last_submission is not None
                    and media_error is None
                    and driver.wait_for_submission(last_submission, max(0, end-time.monotonic())))
        finally:
            try:
                if media:
                    # Snapshot BEFORE our own stop changes halt/worker state.
                    # A failure during the completion wait is still failure.
                    if media_started:
                        media_error = media_error or _cli_media_error(media)
                    media.stop()
                    # Decoder cleanup can publish its final error while joining.
                    if media.error is not None:
                        media_error = media_error or media.error or '媒体解码失败'
            finally:
                while not driver.shutdown(wait_s=0.5):
                    pass
        print(json.dumps({'frames': driver.stats.frames_rendered, 'thread_stopped': True,
                          'submitted_frames': submitted,
                          'last_submission_completed': last_submission_completed,
                          'evidence_dir': str(driver.directory.resolve()), 'mock': args.mock,
                          'media_error': media_error,
                          'decoded': media.decoded if media else None, 'summary': driver.summary}, ensure_ascii=False))
        return 0 if driver.stats.frames_rendered and last_submission_completed is not False and media_error is None and driver.summary and not driver.summary['fatal_error'] and not (args.strict_session and driver.health.offline_events) else 1
    instance = None if args.smoke_ui else SingleInstance()
    if instance and not instance.primary:
        instance.close()
        return 0
    root = tk.Tk()
    app = DashboardApp(root, mock=args.mock or args.smoke_ui, instance=instance)
    smoke_errors = []
    if args.smoke_ui:
        # 实际创建托盘并收起/恢复/退出；不保存设置、不打开串口。
        app.persist = lambda: True
        deadline = time.monotonic() + 8
        def check_tray():
            if app.tray and app.tray.ready.is_set() and root.state() == 'withdrawn':
                app.show_window()
                if root.state() != 'normal':
                    smoke_errors.append('托盘恢复窗口失败')
                app.close()
            elif time.monotonic() >= deadline:
                smoke_errors.append('托盘收起超时：' + app.status.get())
                app.close()
            else:
                root.after(50, check_tray)
        root.after(100, app.hide_to_tray)
        root.after(200, check_tray)
    try:
        root.mainloop()
    finally:
        if instance: instance.close()
    if args.smoke_ui:
        (DEFAULT_OUTPUT / 'smoke-ui-result.json').write_text(json.dumps(
            dict(ok=not smoke_errors, errors=smoke_errors, device_access=False),
            ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if smoke_errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
