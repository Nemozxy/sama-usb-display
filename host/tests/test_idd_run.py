"""真实子进程管道：EOF/截断/挂起/错误洪水/发送失败均需有界收尾。"""
import sys
import time
import threading

from PIL import Image
import pytest

from idd_frame_bridge import LatestFrameMailbox,packet_for
from idd_run import run_process


def process_command(tmp_path, body):
    script=tmp_path/'producer.py'
    script.write_text('import sys,time\n'+body,encoding='utf-8')
    return [sys.executable,str(script)]


def test_cpp_client_offline_end_to_end(tmp_path):
    from idd_run import ROOT,prepare
    raw=(ROOT/'build/host/warp.frame').read_bytes()
    packet=tmp_path/'source.frame';packet.write_bytes(raw)
    command,spec=prepare(packet)
    images=[];reports=[]
    def sink(image):
        images.append(image);report={'frame':len(images),'automatic_retries':0};reports.append(report);return report
    result=run_process(command,sink,maximum_frames=1,source_timeout=5)
    assert result['ok'] and result['source_exit']==0 and result['source_stopped']
    assert result['frames_received']==result['frames_sent']==len(images)==1
    assert result['transmissions']==reports
    assert result['first_transmission']==reports[0]
    assert result['first_receipt']==result['receipts'][0]
    assert result['transmissions_total']==result['transmissions_retained']==1
    assert images[0].getpixel((17,28))==(45,28,17)
    assert not spec['device_access']
    assert result['cancelled'] is False


@pytest.mark.parametrize('fault',['stall','partial','flood','exit','empty'])
def test_bad_producer_is_stopped(tmp_path,fault):
    if fault=='stall':body='time.sleep(30)\n'
    elif fault=='partial':body='sys.stdout.buffer.write(b"SAMAIDD1");sys.stdout.buffer.flush();time.sleep(30)\n'
    elif fault=='flood':body='sys.stderr.buffer.write(b"x"*200000);sys.stderr.buffer.flush();sys.exit(4)\n'
    elif fault=='empty':body='sys.exit(0)\n'
    else:body='sys.exit(3)\n'
    began=time.monotonic()
    result=run_process(process_command(tmp_path,body),lambda _:pytest.fail('不应发送'),
                       source_timeout=.3)
    assert not result['ok'] and result['source_stopped'] and result['frames_sent']==0
    assert time.monotonic()-began<5
    assert len(result['stderr'])<=16384
    if fault=='flood':assert result['stderr']=='x'*16384


def test_sink_failure_terminates_producer_without_retry(tmp_path):
    packet=tmp_path/'frame.bin'
    packet.write_bytes(packet_for(Image.new('RGB',(720,1568),(10,20,30)),1,0))
    command=process_command(tmp_path,'from pathlib import Path\nsys.stdout.buffer.write(Path('+repr(str(packet))+').read_bytes());sys.stdout.buffer.flush();time.sleep(30)\n')
    attempts=[]
    def fail(image):
        attempts.append(image.getpixel((0,0)))
        raise OSError('单次失败')
    result=run_process(command,fail,source_timeout=2)
    assert not result['ok'] and result['source_stopped'] and attempts==[(10,20,30)]
    assert result['frames_sent']==0


def test_abort_discards_waiting_frame():
    from idd_frame_bridge import DesktopFrame
    mailbox=LatestFrameMailbox();mailbox.put(DesktopFrame(1,0,b''));mailbox.abort()
    assert mailbox.take() is None


def test_receipts_are_bounded_without_losing_total():
    from idd_run import ReceiptLog
    log=ReceiptLog()
    for index in range(1000):log.append(dict(sequence=index))
    assert log.total==1000 and len(log.recent)==256
    assert log.recent[0]['sequence']==744
    assert log.first==dict(sequence=0)


@pytest.mark.parametrize('count', [0, 1, 256, 257, 216000])
def test_log_retains_first_and_bounded_tail_for_supported_session_lengths(count):
    from idd_run import ReceiptLog
    log=ReceiptLog()
    for index in range(count):log.append({'sequence':index})
    assert log.total==count and len(log.recent)==min(256,count)
    assert log.first==(dict(sequence=0) if count else None)
    assert [r['sequence'] for r in log.recent]==list(range(max(0,count-256),count))


@pytest.mark.parametrize('none_reports', [False, True])
def test_run_process_bounds_transmissions_and_keeps_first_evidence(tmp_path, monkeypatch, none_reports):
    import idd_run
    # Real subprocess lifecycle, synthetic one-byte source/forward hooks avoid
    # manufacturing 600 full-size desktop images for a metadata retention test.
    def receive(stream, mailbox, maximum_frames):
        assert stream.read()==b'x'
        mailbox.close()
        return 600
    def forward(mailbox, sink, receipts):
        for index in range(600):
            sink(None)
            receipts.append(dict(sequence=index))
    monkeypatch.setattr(idd_run, 'receive', receive)
    monkeypatch.setattr(idd_run, 'forward', forward)
    reports=[]
    def sink(image):
        index=len(reports)
        report=None if none_reports else dict(raw_first_frame=index==0, sequence=index)
        reports.append(report)
        return report
    result=run_process(process_command(tmp_path,"sys.stdout.buffer.write(b'x')"), sink,
        maximum_frames=600, source_timeout=3)
    assert result['ok'] and result['frames_received']==result['frames_sent']==600
    assert result['receipts_retained']==256 and result['first_receipt']==dict(sequence=0)
    assert result['receipts'][0]==dict(sequence=344)
    if none_reports:
        assert result['transmissions']==[] and result['transmissions_total']==0
        assert result['first_transmission'] is None and result['transmissions_retained']==0
    else:
        assert result['transmissions']==reports[-256:]
        assert result['transmissions_total']==600 and result['transmissions_retained']==256
        assert result['first_transmission']==reports[0]
        assert result['first_transmission']['raw_first_frame'] is True
    assert result['transmissions_limit']==result['receipts_limit']==256


def test_user_cancel_stops_producer_without_sending(tmp_path):
    cancel=threading.Event();cancel.set()
    result=run_process(process_command(tmp_path,'time.sleep(30)'),lambda _:pytest.fail('不得发送'),
        source_timeout=5,cancel_event=cancel)
    assert not result['ok'] and result['source_stopped'] and result['frames_sent']==0
    assert '用户取消' in result['errors']
    assert result['cancelled'] is True


@pytest.mark.parametrize('value',['0','-1','86401','junk','1x'])
def test_cpp_client_invalid_limits_do_not_open_driver(value):
    import subprocess
    from idd_run import ROOT
    completed=subprocess.run([str(ROOT/'build/host/frame-client.exe'),'--driver','--seconds',value],
        capture_output=True,timeout=3)
    assert completed.returncode==2 and not completed.stdout
    assert b'unique SAMA' not in completed.stderr


def test_cpp_replay_updates_sequence_and_never_needs_driver():
    from idd_run import ROOT,prepare
    command,spec=prepare(ROOT/'build/host/warp.frame',seconds=1,maximum_frames=3,replay=True)
    result=run_process(command,lambda image:None,maximum_frames=3,source_timeout=3)
    assert result['ok'] and result['frames_received']==3
    assert result['receipts'][-1]['sequence']==3 and spec['source']=='offline_replay'


def test_cpp_replay_respects_source_pacing():
    from idd_run import ROOT,prepare
    command,spec=prepare(ROOT/'build/host/warp.frame',seconds=1,maximum_frames=30,replay=True,source_fps=4)
    result=run_process(command,lambda image:None,maximum_frames=30,source_timeout=3)
    assert result['ok'] and 3<=result['frames_received']<=4
    assert result['frames_sent']<=4 and spec['source_fps']==4


@pytest.mark.parametrize('value',['0','31','-1','junk'])
def test_invalid_cpp_pacing_rejected_before_driver_access(value):
    import subprocess
    from idd_run import ROOT
    completed=subprocess.run([str(ROOT/'build/host/frame-client.exe'),'--driver','--source-fps',value],
        capture_output=True,timeout=3)
    assert completed.returncode==2 and not completed.stdout and not completed.stderr


def test_persistent_sink_one_connection_and_fail_once(tmp_path):
    from idd_run import PersistentFrameSink
    from device import MockTransport
    from types import SimpleNamespace
    class Transport(MockTransport):
        def __init__(self):
            super().__init__([b'chs_65inch.dev1_rom1.91'.ljust(1024,b'\x00')])
            self.fail=False;self.attempts=0;self.closes=0
        def write(self,data):
            self.attempts+=1
            if self.fail:raise OSError('lost')
            return super().write(data)
        def close(self):self.closes+=1;super().close()
    transport=Transport();calls=[]
    sink=PersistentFrameSink(mock=False,output_dir=tmp_path,
        discovery=lambda:[SimpleNamespace(device='FAKE')],
        transport_factory=lambda p:calls.append(p) or transport)
    image=Image.new('RGB',(720,1568),'blue')
    sink(image);sink(image)
    assert calls==['FAKE']
    assert len(b''.join(transport.writes))==250+4534250*2
    transport.fail=True
    with pytest.raises(RuntimeError):sink(image)
    attempts=transport.attempts
    with pytest.raises(RuntimeError):sink(image)
    assert transport.attempts==attempts
    summary=sink.close()
    assert summary['frames_sent']==2 and summary['fatal_error'] and summary['port_released']
    assert sink.close()==summary


def test_cancel_during_sink_finishes_current_call_before_abort(tmp_path):
    packet=tmp_path/'frame.bin'
    packet.write_bytes(packet_for(Image.new('RGB',(720,1568)),1,0))
    command=process_command(tmp_path,'from pathlib import Path'+chr(10)+
        'sys.stdout.buffer.write(Path('+repr(str(packet))+').read_bytes());sys.stdout.buffer.flush();time.sleep(30)')
    cancel=threading.Event();finished=[]
    def sink(image):
        cancel.set();time.sleep(.15);finished.append(True)
    result=run_process(command,sink,source_timeout=5,cancel_event=cancel)
    assert finished==[True] and result['frames_sent']==1 and result['source_stopped']
    assert not result['ok'] and '用户取消' in result['errors']
    assert result['cancelled'] is True


def test_cancel_after_source_eof_during_final_frame_is_not_reported_success(tmp_path):
    packet=tmp_path/'frame.bin'
    packet.write_bytes(packet_for(Image.new('RGB',(720,1568)),1,0))
    command=process_command(tmp_path,'from pathlib import Path\n'+
        'sys.stdout.buffer.write(Path('+repr(str(packet))+').read_bytes());sys.stdout.buffer.flush()')
    cancel=threading.Event();completed=[]
    def sink(image):
        # Force EOF/producer-exit handling to complete while the last send is
        # still in progress. The old watchdog stops watching cancellation here.
        readers=[t for t in threading.enumerate() if t.name=='idd-reader']
        for reader in readers:
            reader.join(3)
            assert not reader.is_alive()
        cancel.set()
        completed.append(True)
        return dict(mock=True, raw_first_frame=True)
    result=run_process(command,sink,maximum_frames=1,source_timeout=5,cancel_event=cancel)
    assert completed==[True] and result['frames_sent']==1
    assert result['source_exit']==0 and result['source_stopped']
    assert result['first_transmission']==dict(mock=True,raw_first_frame=True)
    assert not result['ok'] and '用户取消' in result['errors']
    assert result['cancelled'] is True


def test_cancel_after_eof_preserves_real_mock_cmd200_and_release(tmp_path):
    from idd_run import PersistentFrameSink
    packet=tmp_path/'frame.bin'
    packet.write_bytes(packet_for(Image.new('RGB',(720,1568),(11,22,33)),1,0))
    command=process_command(tmp_path,'from pathlib import Path\n'+
        'sys.stdout.buffer.write(Path('+repr(str(packet))+').read_bytes());sys.stdout.buffer.flush()')
    cancel=threading.Event()
    persistent=PersistentFrameSink(mock=True,output_dir=tmp_path/'transmission')
    def sink(image):
        for reader in [t for t in threading.enumerate() if t.name=='idd-reader']:
            reader.join(3)
            assert not reader.is_alive()
        cancel.set()
        return persistent(image)
    try:
        result=run_process(command,sink,maximum_frames=1,source_timeout=5,cancel_event=cancel)
    finally:
        summary=persistent.close()
    assert result['cancelled'] and not result['ok'] and result['frames_sent']==1
    assert result['source_exit']==0 and result['first_transmission']['raw_first_frame']
    assert summary['frames_sent']==1 and summary['port_released'] and summary['fatal_error'] is None
    wire=(persistent.driver.directory/'connection-001/tx.bin').read_bytes()
    assert len(wire)==4534250
    raw=b''.join(wire[i:i+249] for i in range(250,len(wire),250))[:720*1568*4]
    assert raw==bytes((33,22,11,255))*(720*1568)

def test_broker_prepare_uses_fixed_pipe_entrypoint():
    from pathlib import Path
    from idd_run import prepare,ROOT
    command,spec=prepare(seconds=7,maximum_frames=9,source_fps=4,broker=True)
    assert command[1]=='--broker' and '--driver' not in command
    assert command[-6:]==['--seconds','7','--max-frames','9','--source-fps','4']
    assert spec['source']=='broker' and spec['broker'] and not spec['device_access']
    assert Path(command[0])==ROOT/'build/host/frame-client.exe'


def test_broker_rejects_offline_sources():
    from pathlib import Path
    import pytest
    from idd_run import prepare
    with pytest.raises(ValueError):prepare(Path('x'),broker=True)
    with pytest.raises(ValueError):prepare(None,replay=True,broker=True)
