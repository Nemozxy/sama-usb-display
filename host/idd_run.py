"""有界真实客户端进程→单帧邮箱→现有A发送器；默认只打印零访问计划。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
import signal
from collections import deque

from idd_frame_bridge import LatestFrameMailbox, forward
from idd_stream_bridge import receive
from session_lock import controller_lock

ROOT=Path(__file__).resolve().parents[1]
HOST_BUILD=ROOT/'build'/'host'
DATA=ROOT/'data'


class ReceiptLog:
    """总数独立计数，保留首条证据及最近256条记录，不随时长增长。"""
    def __init__(self):self.recent=deque(maxlen=256);self.total=0;self.first=None
    def append(self,value):
        if self.total==0:self.first=value
        self.total+=1;self.recent.append(value)


class PersistentFrameSink:
    """复用A身份/短写/首帧取证；同步单连接，失败不重连、不重发。"""
    def __init__(self,*,mock,output_dir,discovery=None,transport_factory=None,
                 preferred_port=None,notify=None):
        from dashboard_live import LiveScreen
        self.driver=LiveScreen(mock=mock,output_dir=output_dir,discovery=discovery,
            transport_factory=transport_factory,fail_on_offline=True,
            preferred_port=preferred_port,notify=notify)
        self.started=False;self.closed=False;self.error=None;self.rendered=0;self.first_transmission=None
    def __call__(self,image):
        if self.closed or self.error:raise RuntimeError('帧发送会话已失败或关闭')
        try:
            if not self.started:
                self.started=True
                if not self.driver._connect():raise RuntimeError('USB身份或连接未确认')
            first=self.rendered==0
            if not self.driver._render(image):raise RuntimeError('整帧发送失败，无重试')
            self.rendered+=1
            # 原始TX/RX只保存首帧；后续回执仍有一对一帧序号和像素哈希。
            if self.first_transmission is None:
                capture_path=self.driver.directory/f'connection-{self.driver.connection:03d}'/'capture.json'
                capture=json.loads(capture_path.read_text(encoding='utf-8'))
                self.first_transmission=dict(mock=self.driver.mock,
                    status='mock_completed' if self.driver.mock else 'completed',
                    evidence_dir=str(capture_path.parent),tx_bytes=capture['tx_bytes'],rx_bytes=capture['rx_bytes'],
                    tx_sha256=capture['tx_sha256'],rx_sha256=capture['rx_sha256'],automatic_retries=0)
            return dict(self.first_transmission,raw_first_frame=first)
        except BaseException as exc:
            self.error=repr(exc);raise
    def close(self):
        if self.closed:return self.driver.summary
        self.closed=True
        try:self.driver._close_capture()
        except Exception as exc:
            detail='端口关闭失败：'+repr(exc)
            self.error=(self.error+'; '+detail) if self.error else detail
        if self.driver.evidence_error and self.driver.evidence_error not in (self.error or ''):
            self.error=(self.error+'; ' if self.error else '')+self.driver.evidence_error
        self.driver._screen=None
        self.driver.summary=self.driver.health.report(mock=self.driver.mock,
            dropped=0,released=self.driver.port_release_error is None,error=self.error)
        self.driver.summary.update(rx_bytes_render=self.driver.rx_bytes_render,
                                   rx_chunks_render=self.driver.rx_chunks_render,
                                   last_rx_hex=self.driver.last_rx_hex)
        self.driver.summary['summary_saved']=True
        try:
            (self.driver.directory/'summary.json').write_text(json.dumps(self.driver.summary,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError as exc:
            detail='会话摘要保存失败：'+repr(exc)
            self.error=(self.error+'; '+detail) if self.error else detail
            self.driver.summary.update(summary_saved=False,fatal_error=self.error,
                host_stability_30min_passed=False,host_stability_1hour_passed=False)
        return self.driver.summary


class DifferFrameSink(PersistentFrameSink):
    """Explicit gated differential mode; receipts follow completed writes."""
    def __init__(self, *, mock, output_dir, allow_unverified=False,
                 change_threshold=0, reinit_interval=0, **kwargs):
        if not mock and not allow_unverified:
            raise RuntimeError('实机差分须显式启用 --allow-unverified')
        self.change_threshold = int(change_threshold)
        if not 0 <= self.change_threshold <= 255:
            raise ValueError('差分变化阈值必须在0～255')
        self.reinit_interval = int(reinit_interval)
        if not 0 <= self.reinit_interval <= 300:
            raise ValueError('周期重初始化间隔必须在0～300')
        super().__init__(mock=mock, output_dir=output_dir, **kwargs)
        self.wire_bytes = 0
        self.counts = {'initialize': 0, 'delta': 0, 'full': 0}
        self.pipeline = None

    def _send(self, plan):
        if self.closed or self.error:
            raise RuntimeError('帧发送会话已失败或关闭')
        try:
            if not self.started:
                self.started = True
                if not self.driver._connect():
                    raise RuntimeError('USB身份或连接未确认')
                if (not self.driver.mock and self.driver._screen.identity
                        != 'chs_65inch.dev1_rom1.91'):
                    raise RuntimeError('差分仅允许已确认ROM1.91的本机，拒绝其他设备或ROM')
            from PIL import Image
            image = Image.frombytes('RGBA', (720, 1568), plan.pixels,
                                    'raw', 'BGRA').convert('RGB')
            first = self.rendered == 0
            if not self.driver._render(image, wire=plan.data):
                raise RuntimeError('差分发送失败，无重试')
            self.rendered += 1
            self.wire_bytes += plan.wire_bytes
            self.counts[plan.kind] += 1
            if self.first_transmission is None:
                path = self.driver.directory / f'connection-{self.driver.connection:03d}' / 'capture.json'
                capture = json.loads(path.read_text(encoding='utf-8'))
                self.first_transmission = dict(
                    mock=self.driver.mock,
                    status='mock_completed' if self.driver.mock else 'host_send_completed',
                    evidence_dir=str(path.parent), tx_bytes=capture['tx_bytes'],
                    rx_bytes=capture['rx_bytes'], tx_sha256=capture['tx_sha256'],
                    rx_sha256=capture['rx_sha256'], automatic_retries=0,
                    visual_confirmation='pending')
            return dict(self.first_transmission, raw_first_frame=first, mode=plan.kind)
        except BaseException as exc:
            self.error = repr(exc)
            raise

    def forward(self, mailbox, receipts, before_send, transmissions):
        from differ_pipeline import DifferPipeline, DifferPlanner
        def send(plan):
            report = self._send(plan)
            if report is not None:
                transmissions.append(report)
        planner = DifferPlanner(change_threshold=self.change_threshold,
                                reinit_interval=self.reinit_interval)
        self.pipeline = DifferPipeline(send, planner=planner)
        result = self.pipeline.forward(mailbox, receipts, before_send)
        self.encoded_overwritten = self.pipeline.encoded_overwritten
        return result

    def close(self):
        summary = super().close()
        summary.update(protocol='2xcmd200 init + monotonic cmd204; cmd200 fallback',
                       visual_confirmation='pending', hardware_verified=False,
                       wire_image_bytes=self.wire_bytes,
                       differ_counts=dict(self.counts),
                       change_threshold=self.change_threshold,
                       reinit_interval=self.reinit_interval,
                       source_pixel_exact=self.change_threshold == 0,
                       encoded_overwritten=getattr(self, 'encoded_overwritten', 0),
                       scope='主机发送完成；不是实体屏目视或实机fps验收')
        if summary.get('summary_saved'):
            (self.driver.directory / 'summary.json').write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        return summary


def run_process(command, sink, *, maximum_frames=120, source_timeout=35, cancel_event=None,
                expected_client_sha256=None):
    """超时关闭生产进程；USB整帧发送遵循原发送器的I/O时限，不中途截断。"""
    if not 1<=maximum_frames<=2592000 or not .1<=source_timeout<=86405:
        raise ValueError('只允许1～2592000帧、0.1～86405秒生产进程期限')
    if expected_client_sha256 is not None:
        if hashlib.sha256(Path(command[0]).read_bytes()).hexdigest()!=expected_client_sha256:
            raise ValueError('FrameClient启动前SHA256不匹配')
    mailbox=LatestFrameMailbox()
    finished=threading.Event();aborted=threading.Event()
    errors=[];diagnostic=bytearray();received=[];receipts=ReceiptLog()
    transmissions=ReceiptLog();helpers=[]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    started=time.monotonic()

    def abort(message):
        errors.append(message);aborted.set();mailbox.abort()
        if process.poll() is None:
            try:process.terminate()
            except OSError:pass

    def read_stderr():
        # Drain continuously so an error flood cannot deadlock stdout. Keep a
        # bounded diagnostic prefix; never mix text into the binary frame pipe.
        try:
            while chunk:=process.stderr.read(4096):
                diagnostic.extend(chunk[:max(0,16384-len(diagnostic))])
        except OSError as exc:
            if not aborted.is_set():abort('stderr: '+str(exc))

    def read_frames():
        try:
            received.append(receive(process.stdout,mailbox,maximum_frames))
            code=process.wait(timeout=2)
            if code:abort('生产进程退出码：'+str(code))
            elif not received[0]:abort('生产进程正常退出但未返回任何帧')
        except Exception as exc:
            if not aborted.is_set():abort('帧流：'+str(exc))
        finally:finished.set()

    def watchdog():
        while not finished.wait(.05):
            if cancel_event is not None and cancel_event.is_set():
                abort('用户取消');return
            if time.monotonic()-started>=source_timeout:
                abort('生产进程超时');return

    phase='管道初始化'
    try:
        # Own the child from the moment Popen succeeds, including partial
        # thread construction/startup. Never join a thread that did not start.
        for target,name in ((read_stderr,'idd-stderr'),(read_frames,'idd-reader'),
                            (watchdog,'idd-deadline')):
            helper=threading.Thread(target=target,name=name)
            helpers.append(helper)
            helper.start()
        phase='发送'
        def check_ready():
            if aborted.is_set():raise RuntimeError('帧源已中止')
            if cancel_event is not None and cancel_event.is_set():raise RuntimeError('用户取消')
        def checked_sink(image):
            check_ready()
            report=sink(image)
            if report is not None:transmissions.append(report)
        if isinstance(sink, DifferFrameSink):
            sink.forward(mailbox, receipts, check_ready, transmissions)
        else:
            forward(mailbox,checked_sink,receipts)
    except BaseException as exc:
        abort(phase+'：'+str(exc))
    finally:
        if process.poll() is None:
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:abort('生产进程未退出')
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        finished.set();mailbox.abort()
        for helper in helpers:
            if helper.ident is not None:helper.join(3)
        process.stdout.close();process.stderr.close()
    if any(helper.is_alive() for helper in helpers):
        raise RuntimeError('生产进程管道线程未收尾')
    # The source deadline/watchdog ends at EOF, but cancellation remains valid
    # while the final sink call or cleanup is finishing. Preserve its completed
    # frame/receipt; do not turn that into an uncancelled successful session.
    cancelled=cancel_event is not None and cancel_event.is_set()
    if cancelled and '用户取消' not in errors:
        errors.append('用户取消')
    client_unchanged=None
    if expected_client_sha256 is not None:
        try:client_unchanged=hashlib.sha256(Path(command[0]).read_bytes()).hexdigest()==expected_client_sha256
        except OSError:client_unchanged=False
        if not client_unchanged:errors.append('FrameClient运行后SHA256不匹配或文件不可读')
    return dict(ok=not errors,errors=errors,source_exit=process.returncode,
        source_stopped=True,cancelled=cancelled,client_unchanged=client_unchanged,
        frames_received=received[0] if received else mailbox.accepted,
        frames_sent=receipts.total,mailbox_overwritten=mailbox.overwritten,receipts=list(receipts.recent),
        transmissions=list(transmissions.recent),
        transmissions_total=transmissions.total,
        transmissions_retained=len(transmissions.recent),transmissions_limit=256,
        first_transmission=transmissions.first,first_receipt=receipts.first,
        receipts_retained=len(receipts.recent),receipts_limit=256,
        stderr=diagnostic.decode('utf-8',errors='replace'),elapsed_seconds=time.monotonic()-started)


def prepare(packet_file=None,*,seconds=30,maximum_frames=120,replay=False,source_fps=30,broker=False,
            client_executable=None,client_sha256=None):
    if not 1<=seconds<=86400 or not 1<=maximum_frames<=2592000:
        raise ValueError('时长须1～86400秒，帧数须1～2592000')
    if replay and packet_file is None:raise ValueError('离线循环须指定帧文件')
    if broker and (packet_file is not None or replay):
        raise ValueError('broker不得同时指定离线包或replay')
    if not 1 <= source_fps <= 30:raise ValueError('客户端取帧速率须1～30fps')
    if (client_executable is None)!=(client_sha256 is None):
        raise ValueError('显式客户端路径和预期SHA256须同时提供')
    custom=client_executable is not None
    if custom and (len(client_sha256)!=64 or any(c not in '0123456789abcdefABCDEF' for c in client_sha256)):
        raise ValueError('客户端SHA256须为64位十六进制')
    client=Path(client_executable).resolve() if custom else HOST_BUILD/'frame-client.exe'
    if not client.is_file():raise ValueError('先构建FrameClient')
    actual_sha256=hashlib.sha256(client.read_bytes()).hexdigest()
    if custom and actual_sha256!=client_sha256.lower():raise ValueError('FrameClient预期SHA256不匹配')
    if packet_file is None:
        command=[str(client),'--broker' if broker else '--driver']
    else:
        command=[str(client),'--replay-file' if replay else '--check-file',str(Path(packet_file).resolve())]
    if packet_file is None or replay:
        command+=['--seconds',str(seconds),'--max-frames',str(maximum_frames),'--source-fps',str(source_fps)]
    return command,dict(client_sha256=actual_sha256,client_executable=str(client),client_explicit=custom,
        source='broker' if broker else 'driver' if packet_file is None else 'offline_replay' if replay else 'offline_file',
        maximum_frames=maximum_frames,source_fps=source_fps,
        source_timeout_seconds=seconds+5,source_seconds=seconds,device_access=False,desktop_capture=False,
        desktop_capture_attempted=False,broker=broker,
        note='默认仅计划；--run启动客户端，--usb另需实机发送授权。broker入口供普通用户经固定只读协议取帧；单连接/首帧原始取证/最近256回执及传输记录，生产进程期限不截断正在发送的USB整帧。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet-file',type=Path,help='离线SAMAIDD1包；省略则准备真实驱动入口')
    parser.add_argument('--run',action='store_true',help='启动生产进程；真实驱动入口须先获授权')
    parser.add_argument('--usb',action='store_true',help='真实USB发送，须另获有界发图授权；默认模拟')
    parser.add_argument('--output',type=Path,default=DATA/'idd-run')
    parser.add_argument('--seconds',type=int,default=30,help='显式运行时长，1～86400秒；默认30')
    parser.add_argument('--max-frames',type=int,default=120,help='源帧上限1～2592000；默认120')
    parser.add_argument('--replay',action='store_true',help='只循环指定离线包，不枚举或打开驱动')
    parser.add_argument('--broker',action='store_true',help='普通用户经SamaFrameBroker固定GetFrame管道取帧；不能与离线包同用')
    parser.add_argument('--source-fps',type=int,default=30,help='客户端取帧速率1～30；慢USB可显式设4，不改变Windows显示模式')
    parser.add_argument('--client',type=Path,help='显式选择FrameClient候选；须同时指定 --client-sha256，不替换默认客户端')
    parser.add_argument('--client-sha256',help='显式客户端预期SHA256；计划、启动前和运行后核对')
    parser.add_argument('--differ',action='store_true',help='显式启用差分流水线；默认cmd200')
    parser.add_argument('--differ-change-threshold',type=int,default=0,
                        help='有损BGR小变化过滤阈值0～255；默认0保持源像素精确')
    parser.add_argument('--differ-reinit-interval',type=int,default=0,
                        help='每N条差分强制双200重初始化，0～300；默认0不周期重置')
    parser.add_argument('--allow-unverified',action='store_true',help='与--usb --differ同用才允许实机差分；沿用历史参数名')
    args=parser.parse_args()
    command,spec=prepare(args.packet_file,seconds=args.seconds,maximum_frames=args.max_frames,
        replay=args.replay,source_fps=args.source_fps,broker=args.broker,
        client_executable=args.client,client_sha256=args.client_sha256)
    if args.usb and not args.run:parser.error('--usb须同时--run，且先获实机授权')
    if args.allow_unverified and not args.differ:parser.error('--allow-unverified仅用于--differ')
    if not 0<=args.differ_change_threshold<=255:parser.error('--differ-change-threshold须为0～255')
    if args.differ_change_threshold and not args.differ:
        parser.error('--differ-change-threshold仅用于--differ')
    if not 0<=args.differ_reinit_interval<=300:parser.error('--differ-reinit-interval须为0～300')
    if args.differ_reinit_interval and not args.differ:
        parser.error('--differ-reinit-interval仅用于--differ')
    if args.usb and args.differ and not args.allow_unverified:
        parser.error('实机差分须同时指定--differ --allow-unverified')
    spec.update(differ=args.differ,change_threshold=args.differ_change_threshold,
                reinit_interval=args.differ_reinit_interval,
                source_pixel_exact=args.differ_change_threshold==0,
                visual_confirmation='pending',hardware_verified=not args.differ)
    if not args.run:
        print(json.dumps(spec,ensure_ascii=True,indent=2));return 0
    # Each attempt owns its evidence directory, including failures.
    session=args.output/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    session.mkdir(parents=True,exist_ok=False)
    def execute():
        options = dict(mock=not args.usb, output_dir=session/'transmissions')
        if args.differ:
            options['allow_unverified'] = args.allow_unverified
            options['change_threshold'] = args.differ_change_threshold
            options['reinit_interval'] = args.differ_reinit_interval
            sink = DifferFrameSink(**options)
        else:
            sink = PersistentFrameSink(**options)
        cancel=threading.Event()
        previous=signal.signal(signal.SIGINT,lambda signum,frame:cancel.set())
        try:result=run_process(command,sink,maximum_frames=args.max_frames,
            source_timeout=spec['source_timeout_seconds'],cancel_event=cancel,
            expected_client_sha256=spec.get('client_sha256'))
        except Exception as exc:
            result=dict(ok=False,errors=[repr(exc)],frames_received=0,frames_sent=0,source_stopped=False)
        finally:
            signal.signal(signal.SIGINT,previous)
            try:
                summary=sink.close()
            except Exception as exc:
                # Preserve producer/frame evidence even if close itself fails.
                # With no completed summary, release is unknown: never invent
                # success from an absent/partially-built report.
                summary=dict(sink.driver.summary or dict(mock=not args.usb,
                    device_access=args.usb,frames_sent=result['frames_sent'],port_released=False))
                detail='USB会话收尾异常：'+repr(exc)
                prior=summary.get('fatal_error')
                summary.update(summary_saved=False,fatal_error=(str(prior)+'; '+detail) if prior else detail,
                    host_stability_30min_passed=False,host_stability_1hour_passed=False)
        result.update(usb_summary=summary,usb_evidence_dir=str(sink.driver.directory))
        if summary['fatal_error'] or not summary['port_released'] or summary.get('summary_saved') is False:
            result['ok']=False;result['errors'].append('USB会话或关闭失败')
        return result
    if args.usb:
        with controller_lock(DATA):result=execute()
    else:result=execute()
    result.update(spec,ok=result['ok'],device_access=args.usb,
                  desktop_capture_attempted=args.packet_file is None and not args.replay,
                  desktop_capture=args.packet_file is None and not args.replay and result['frames_received']>0,
                  evidence_dir=str(session),
                  packet_file=str(args.packet_file.resolve()) if args.packet_file else None)
    (session/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    # JSON remains lossless, but escaped Unicode is safe for Windows redirected
    # stdout using a legacy code page (including paths with surrogate pairs).
    print(json.dumps({k:v for k,v in result.items() if k!='transmissions'},ensure_ascii=True,indent=2))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
