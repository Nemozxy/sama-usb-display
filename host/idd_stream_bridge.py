"""C：有界BGRA字节流→容量1邮箱→模拟USB。演示源为独立子进程测试图。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from idd_frame_bridge import (HEADER, DesktopFrame, LatestFrameMailbox, payload_size,
                              packet_for, forward)


def read_exact(stream, size, allow_eof=False):
    data = bytearray(size)
    offset = 0
    while offset < size:
        chunk = stream.read(size-offset)
        if not chunk:
            if allow_eof and offset == 0:
                return None
            raise ValueError('IDD字节流在帧中间结束')
        if len(chunk) > size-offset:
            raise ValueError('流返回数据超过请求')
        data[offset:offset+len(chunk)] = chunk
        offset += len(chunk)
    return bytes(data)


def receive(stream, mailbox, maximum_frames):
    if not 1 <= maximum_frames <= 2592000:
        raise ValueError('帧数上限须为1至2592000')
    count = 0
    try:
        while count < maximum_frames:
            header = read_exact(stream, HEADER.size, allow_eof=True)
            if header is None:
                break
            # Validate before allocating/reading the payload. No magic scanning
            # or automatic resynchronization of a corrupt/truncated stream.
            size = payload_size(header)
            pixels = read_exact(stream, size)
            mailbox.put(DesktopFrame.from_packet(header+pixels))
            count += 1
        if count == maximum_frames and stream.read(1):
            raise ValueError('IDD字节流超过帧数上限')
    except BaseException:
        # A corrupt stream must not release an old pending frame as if EOF had
        # been clean. Abort before waking consumers/caller error handling; the
        # latter otherwise races a normal close that still exposes _pending.
        mailbox.abort()
        raise
    else:
        mailbox.close()
        return count


def demo(output):
    from dashboard_backend import send_once
    mailbox = LatestFrameMailbox()
    receipts, transports, errors = [], [], []

    def send():
        try:
            receipts.extend(forward(mailbox, lambda image: transports.append(
                send_once(image,mock=True,output_dir=output))))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=send,daemon=True)
    worker.start()
    # Inherited anonymous pipe, no network listener, desktop capture or driver.
    process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--produce'],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        count = receive(process.stdout,mailbox,3)
        code = process.wait(timeout=10)
        diagnostic = process.stderr.read().decode('utf-8',errors='replace')
        assert code == 0, diagnostic
    finally:
        mailbox.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        worker.join(10)
    assert not worker.is_alive() and not errors, errors
    assert count == 3 and receipts and receipts[-1]['sequence'] == 3
    report = dict(ok=True,real_driver=False,device_access=False,source='独立子进程生成的测试图',
                  frames_received=count,mailbox_overwritten=mailbox.overwritten,
                  receipts=receipts,transport_count=len(transports))
    output.mkdir(parents=True,exist_ok=True)
    (output/'stream-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--produce',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'data'/'idd-stream')
    args=parser.parse_args()
    if args.produce:
        # On Windows, binary stdout avoids CR/LF translation by text wrappers.
        from dashboard_render import test_card
        image=test_card()
        for sequence in range(1,4):
            sys.stdout.buffer.write(packet_for(image,sequence,time.monotonic_ns(),pitch=3072))
            sys.stdout.buffer.flush()
    else:
        print(json.dumps(demo(args.output),ensure_ascii=False,indent=2))
