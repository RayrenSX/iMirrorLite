"""Video size limits are independent from RTSP and follow the actual codec dimensions."""
import asyncio
import struct
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/src/main/python'))
from imirror.airplay import MAX_BODY, Receiver, Session, mirror_cipher, video_packet_limit


def packet(kind, payload=b'', size=None, width=3840, height=2160):
    header=bytearray(128)
    struct.pack_into('<I',header,0,len(payload) if size is None else size)
    header[4]=kind
    struct.pack_into('<ff',header,56,width,height)
    return bytes(header)+payload


def configuration(width=3840,height=2160):
    sps,pps=bytes.fromhex('6742001e95a814016e40'),bytes.fromhex('68ce06e2')
    avcc=b'\x01'+sps[1:4]+b'\xff\xe1'+struct.pack('>H',len(sps))+sps+b'\x01'+struct.pack('>H',len(pps))+pps
    return packet(1,avcc,width=width,height=height)


class VideoPacketLimitTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        loop=asyncio.get_running_loop()
        self.received=asyncio.Event()
        self.diagnostics=[]; self.frames=[]; self.disconnected=[]
        def decode(*args):
            self.frames.append(args)
            loop.call_soon_threadsafe(self.received.set)
        runtime=SimpleNamespace(stop=threading.Event(),clear_peer=lambda host:None,
            status=lambda *args:None,on_video=decode,
            callback=SimpleNamespace(onDiagnostic=self.diagnostics.append,
                onVideoDisconnected=lambda:self.disconnected.append(True)))
        receiver=SimpleNamespace(runtime=runtime,owner=None,audio_sinks=[])
        session=Session(receiver,'127.0.0.1'); receiver.owner=session
        session.decryptor=mirror_cipher(bytes(16),123)
        session.legacy_decryptor=mirror_cipher(bytes(16),123)
        session.key_mode=None
        return session

    @staticmethod
    def writer():
        class Writer:
            def get_extra_info(self,key): return ('127.0.0.1',1234)
            def close(self): pass
        return Writer()

    async def test_encrypted_large_idr_is_delivered_after_4k_configuration(self):
        session=self.session()
        # The decoder is stubbed here: exercise NAL framing and streaming AES,
        # with a packet above the old 2 MiB RTSP limit, before any codec call.
        nal=b'\x65'+b'\x88'*(4*1024*1024)
        wire=struct.pack('>I',len(nal))+nal
        encrypted=mirror_cipher(bytes(16),123).update(wire)
        reader=asyncio.StreamReader()
        task=asyncio.create_task(session.video(reader,self.writer()))
        try:
            reader.feed_data(configuration()+packet(0,encrypted))
            await asyncio.wait_for(self.received.wait(),2)
            self.assertEqual(self.frames[0][0],b'\0\0\0\1'+nal)
            self.assertEqual(self.frames[0][2:4],(3840,2160))
            self.assertFalse(any('too large' in text for text in self.diagnostics))
        finally:
            reader.feed_eof()
            await asyncio.wait_for(task,2)
        self.assertEqual(len(self.disconnected),1)
        self.assertIsNone(session.receiver.owner)

    async def reject(self,data):
        session=self.session(); reader=asyncio.StreamReader(); reader.feed_data(data)
        # No payload follows an oversized header. Rejection must precede
        # allocation and waiting for the declared body.
        await asyncio.wait_for(session.video(reader,self.writer()),2)
        self.assertTrue(any('too large' in text for text in self.diagnostics),self.diagnostics)
        self.assertFalse(self.frames)
        self.assertEqual(len(self.disconnected),1)

    async def test_video_limit_uses_actual_dimensions(self):
        await self.reject(configuration(640,960)+packet(0,size=MAX_BODY+1,width=640,height=960))
        self.assertTrue(any('kind=0' in text and f'bytes={MAX_BODY+1}' in text for text in self.diagnostics))

    async def test_4k_packet_above_decoder_input_budget_is_rejected(self):
        await self.reject(configuration()+packet(0,size=3840*2160+1))

    async def test_unconfigured_video_and_configuration_keep_small_limit(self):
        await self.reject(packet(0,size=MAX_BODY+1))
        await self.reject(packet(1,size=MAX_BODY+1))

    async def test_video_has_absolute_memory_limit_even_with_maximum_dimensions(self):
        await self.reject(configuration(8192,8192)+packet(0,size=32*1024*1024+1))

    async def test_decoder_callback_finishes_before_session_close_releases_owner(self):
        session=self.session()
        started=asyncio.Event(); completed=[]; release=threading.Event()
        loop=asyncio.get_running_loop()
        def decode(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(2): raise TimeoutError('Test did not release decode callback')
            completed.append(True)
        session.runtime.on_video=decode
        session.runtime.callback.onVideoDisconnected=lambda:self.disconnected.append(bool(completed))
        reader=asyncio.StreamReader()
        wire=b'\0\0\0\5\x65test'
        reader.feed_data(configuration()+packet(0,mirror_cipher(bytes(16),123).update(wire)))
        task=asyncio.create_task(session.video(reader,self.writer()))
        closing=None
        try:
            await asyncio.wait_for(started.wait(),1)
            closing=asyncio.create_task(session.close())
            # Let close reach cancellation and decoder-worker cleanup.
            for _ in range(5): await asyncio.sleep(0)
            self.assertFalse(closing.done(),'Close detached an active codec callback')
            self.assertIs(session.receiver.owner,session)
            release.set()
            await asyncio.wait_for(closing,2)
            await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(self.disconnected,[True])
            self.assertIsNone(session.receiver.owner)
            self.assertIsNone(session.decode_task)
            self.assertFalse(session.tasks)
            await session.close()  # Repeated cleanup remains harmless.
            self.assertEqual(self.disconnected,[True])
        finally:
            release.set()
            if closing is not None: await asyncio.gather(closing,return_exceptions=True)
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_rtsp_control_body_limit_remains_small(self):
        self.session()
        import tempfile
        class Writer:
            def get_extra_info(self,key): return ('127.0.0.1',1234)
            def close(self): pass
        with tempfile.TemporaryDirectory() as root:
            runtime=SimpleNamespace(root=root,stop=threading.Event(),
                callback=SimpleNamespace(onDiagnostic=self.diagnostics.append))
            receiver=Receiver(runtime,'test')
            reader=asyncio.StreamReader()
            reader.feed_data(f'POST /info RTSP/1.0\r\nContent-Length: {MAX_BODY+1}\r\n\r\n'.encode())
            await asyncio.wait_for(receiver.handle(reader,Writer()),1)
            self.assertTrue(any('AirPlay request too large' in text for text in self.diagnostics))
            self.assertFalse(receiver.tasks)
            self.assertFalse(receiver.sessions)


class PacketBudgetTests(unittest.TestCase):
    def test_supported_canvases_and_portrait_rotation(self):
        for height in (540,720,1080,1440,2160,2880):
            width=height*16//9
            self.assertEqual(video_packet_limit(width,height),video_packet_limit(height,width))
            self.assertGreaterEqual(video_packet_limit(width,height),MAX_BODY)
            self.assertLessEqual(video_packet_limit(width,height),32*1024*1024)


if __name__=='__main__': unittest.main()
