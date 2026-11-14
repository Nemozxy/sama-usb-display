import io
import struct
import threading

import pytest
from PIL import Image
from idd_frame_bridge import WIDTH,HEIGHT,HEADER,LatestFrameMailbox,packet_for
from idd_stream_bridge import receive
from idd_frame_bridge import forward


class Fragmented(io.BytesIO):
    def read(self,size=-1):
        return super().read(min(size,997))


def test_fragmented_and_coalesced_stream_keeps_latest_owned_pixels():
    source=Image.new('RGB',(WIDTH,HEIGHT),(20,60,180))
    packet=packet_for(source,1,0,pitch=3072)
    second=packet_for(source,2,1)
    mailbox=LatestFrameMailbox()
    assert receive(Fragmented(packet+second),mailbox,3)==2
    frame=mailbox.take()
    assert frame.sequence==2 and frame.image().tobytes()==source.tobytes()
    assert mailbox.overwritten==1 and mailbox.take() is None


@pytest.mark.parametrize('fault',['header','payload','size','sequence','budget'])
def test_bad_stream_stops_without_resynchronizing(fault):
    packet=packet_for(Image.new('RGB',(WIDTH,HEIGHT)),1,0)
    stream=io.BytesIO(packet)
    if fault=='header':stream=io.BytesIO(packet[:12])
    elif fault=='payload':stream=io.BytesIO(packet[:-1])
    elif fault=='size':
        header=bytearray(packet[:HEADER.size]);struct.pack_into('<I',header,16,0xfffffffc)
        stream=io.BytesIO(header+b'never read')
    elif fault in ('sequence','budget'):stream=io.BytesIO(packet+packet)
    mailbox=LatestFrameMailbox()
    with pytest.raises(ValueError):receive(stream,mailbox,1 if fault=='budget' else 3)
    if fault=='size':assert stream.tell()==HEADER.size
    # Exceptional receive aborts pending frames and wakes the consumer.
    mailbox.take()
    assert mailbox.take() is None


def test_normal_eof_preserves_complete_pending_frame():
    packet=packet_for(Image.new('RGB',(WIDTH,HEIGHT)),1,0)
    mailbox=LatestFrameMailbox()
    assert receive(io.BytesIO(packet),mailbox,3)==1
    assert mailbox.take().sequence==1
    assert mailbox.take() is None


def test_corruption_drops_pending_but_finishes_inflight_frame():
    first=packet_for(Image.new('RGB',(WIDTH,HEIGHT),'red'),1,0)
    second=packet_for(Image.new('RGB',(WIDTH,HEIGHT),'blue'),2,0)
    entered,release=threading.Event(),threading.Event()
    mailbox=LatestFrameMailbox()
    sent,receipts,errors=[],[],[]
    class PauseAfterFirst(io.BytesIO):
        def read(self,size=-1):
            if self.tell()==len(first):
                assert entered.wait(3)
            return super().read(size)
    def sink(image):
        entered.set()
        assert release.wait(3)
        sent.append(image.getpixel((0,0)))
    def consume():
        try:forward(mailbox,sink,receipts)
        except BaseException as exc:errors.append(exc)
    worker=threading.Thread(target=consume)
    worker.start()
    try:
        with pytest.raises(ValueError):
            receive(PauseAfterFirst(first+second+b'SAMA'),mailbox,3)
        assert mailbox.accepted==2 and sent==[]
    finally:
        release.set()
        mailbox.abort()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert sent==[(255,0,0)] and [r['sequence'] for r in receipts]==[1]


@pytest.mark.parametrize('fault', ['truncated_header', 'truncated_payload', 'duplicate', 'budget'])
def test_corrupt_stream_discards_complete_but_not_yet_sent_frame(fault):
    packet=packet_for(Image.new('RGB',(WIDTH,HEIGHT)),1,0)
    if fault=='truncated_header':tail=b'SAMA'
    elif fault=='truncated_payload':tail=packet[:HEADER.size+12]
    else:tail=packet
    mailbox=LatestFrameMailbox()
    with pytest.raises(ValueError):
        receive(io.BytesIO(packet+tail),mailbox,1 if fault=='budget' else 3)
    assert mailbox.accepted==1
    assert mailbox.take() is None


@pytest.mark.parametrize('exception', [OSError('read failed'), KeyboardInterrupt()])
def test_read_exception_aborts_pending_before_it_reaches_caller(exception):
    packet=packet_for(Image.new('RGB',(WIDTH,HEIGHT)),1,0)
    class FailAfterFirst(io.BytesIO):
        def read(self,size=-1):
            if self.tell()==len(packet):raise exception
            return super().read(size)
    mailbox=LatestFrameMailbox()
    with pytest.raises(type(exception)):
        receive(FailAfterFirst(packet),mailbox,3)
    assert mailbox.accepted==1 and mailbox.take() is None
