"""AirPlay 1 screen receiver. Wire protocol adapted from pinned UxPlay (GPL-3.0).

Only H.264 mirroring is advertised. Android NSD publishes the receiver; the
private PlayFair JNI unwraps the stream key. No audio or media playback UI.
"""
import asyncio
import hashlib
import ipaddress
import json
import math
import os
import plistlib
import secrets
import socket
import struct
import time
import uuid
from contextlib import suppress
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from srptools import SRPContext, SRPServerSession
from srptools.constants import PRIME_2048, PRIME_2048_GEN
from imirror.quicktime import parameter_sets, read_nal

MAX_BODY = 2 * 1024 * 1024  # RTSP bodies and codec configuration, not video frames.
MAX_VIDEO_PACKET = 32 * 1024 * 1024
FEATURES = 0x5A7FFEE6 & ~(1 << 0)  # No HLS/video-file player.


def peer_host(address):
    value = ipaddress.ip_address(address.split('%',1)[0])
    return str(getattr(value,'ipv4_mapped',None) or value)


async def listen(callback, **kwargs):
    # Android NSD publishes both A and AAAA records. One dual-stack listener
    # must accept both addresses on the same advertised port.
    sock = None
    try:
        sock = socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0)
        sock.bind(('::',0)); sock.setblocking(False)
        return await asyncio.start_server(callback,sock=sock,**kwargs)
    except OSError:
        if sock: sock.close()
        return await asyncio.start_server(callback,'0.0.0.0',0,**kwargs)


class PinContext(SRPContext):
    """Apple's legacy SRP6a: SHA1, 2048-bit group and 40-byte session key."""
    def get_common_session_key(self, premaster_secret):
        secret = premaster_secret.to_bytes((premaster_secret.bit_length()+7)//8,'big')
        return hashlib.sha1(secret+b'\0'*4).digest()+hashlib.sha1(secret+b'\0\0\0\1').digest()

    def get_common_session_key_proof(self, session_key, salt, server_public, client_public):
        xor = bytes(a^b for a,b in zip(self.hash(self._prime,as_bytes=True),self.hash(self._gen,as_bytes=True)))
        return self.hash(xor,self.hash(self._user,as_bytes=True),salt,client_public,server_public,session_key,as_bytes=True)


def identity(root):
    path = Path(root) / 'airplay.key'
    if not path.exists():
        path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
        with suppress(OSError): path.chmod(0o600)
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def mirror_cipher(key, stream_id):
    sid = str(int(stream_id)).encode()
    derived = hashlib.sha512(b'AirPlayStreamKey' + sid + key).digest()[:16]
    iv = hashlib.sha512(b'AirPlayStreamIV' + sid + key).digest()[:16]
    return Cipher(algorithms.AES(derived), modes.CTR(iv)).decryptor()


def unwrap_key(message, encrypted):
    from java import jarray, jbyte, jclass
    native = jclass('com.rayren.imirrorlite.AirPlayCrypto')
    return bytes(native.unwrap(jarray(jbyte)(message), jarray(jbyte)(encrypted)))


def codec_packet(header, payload):
    if len(header) != 128 or len(payload) < 7 or payload[0] != 1:
        raise ValueError('Invalid AirPlay H.264 configuration')
    length_size, csd = parameter_sets(payload, False)
    if not csd: raise ValueError('Empty AirPlay H.264 configuration')
    width, height = struct.unpack_from('<ff', header, 56)
    if not all(math.isfinite(x) and 16 <= x <= 8192 for x in (width, height)):
        raise ValueError('Invalid AirPlay video dimensions')
    return csd, int(width), int(height), length_size


def video_packet_limit(width, height):
    # Match MediaCodec's max-input-size hint. Bound before reading/allocating
    # the payload, even if the sender reports the maximum supported dimensions.
    return min(MAX_VIDEO_PACKET, max(MAX_BODY, width * height))


def annex_b(data, length_size=4):
    result = bytearray()
    offset = 0
    while offset < len(data):
        nal, offset = read_nal(data, offset, length_size)
        if not nal or nal[0]&0x80 or not 1<=nal[0]&31<=23:
            raise ValueError('Invalid AirPlay H.264 NAL header')
        result.extend(b'\0\0\0\1' + nal)
    return bytes(result)


class Timing(asyncio.DatagramProtocol):
    def connection_made(self, transport): self.transport = transport

    def datagram_received(self, data, address):
        if len(data) != 32 or data[1] & 0x7f != 82: return
        now = time.time() + 2208988800
        stamp = struct.pack('>Q', int(now * 2**32))
        self.transport.sendto(b'\x80\xd3' + data[2:4] + b'\0'*4 + data[24:32] + stamp + stamp, address)


class Session:
    def __init__(self, receiver, peer):
        address = peer if isinstance(peer,tuple) else (peer,0)
        self.receiver, self.runtime, self.peer = receiver, receiver.runtime, peer_host(address[0])
        self.scope = address[3] if len(address)>3 else 0
        self.shared = None
        self.verified = False
        self.keymsg = None
        self.key = None
        self.base_key = None
        self.pin = None
        self.pin_at = 0
        self.srp = None
        self.pin_verified = False
        self.video_server = None
        self.event_server = None
        self.timing = None
        self.timing_task = None
        self.audio_sinks = []
        self.writers = set()
        self.tasks = set()
        self.video_writer = None
        self.closed = False
        self.decode_task = None

    async def close(self):
        if self.closed: return
        self.closed = True
        for server in (self.video_server, self.event_server):
            if server:
                server.close()
        if self.timing: self.timing.close()
        for transport in self.audio_sinks:
            transport.close()
            if transport in self.receiver.audio_sinks: self.receiver.audio_sinks.remove(transport)
        self.audio_sinks.clear()
        if self.timing_task:
            self.timing_task.cancel()
            await asyncio.gather(self.timing_task, return_exceptions=True)
        for writer in list(self.writers): writer.close()
        tasks = [task for task in self.tasks if task != asyncio.current_task()]
        for task in tasks:
            if not task.cancelling(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in (self.video_server, self.event_server):
            if server: await server.wait_closed()
        if self.receiver.owner is self:
            self.receiver.owner = None
            self.runtime.clear_peer(self.peer)

    def pair_verify(self, body):
        if len(body) == 68 and body[0] == 1:
            self.verified = False
            self.keymsg = self.key = None
            self.peer_ecdh, self.peer_ed = body[4:36], body[36:68]
            if self.peer_ed.hex() not in self.receiver.clients:
                self.shared = None
                raise PermissionError('请先使用安卓显示的 AirPlay 配对码完成配对')
            private = X25519PrivateKey.generate()
            self.public = private.public_key().public_bytes_raw()
            self.shared = private.exchange(X25519PublicKey.from_public_bytes(self.peer_ecdh))
            key = hashlib.sha512(b'Pair-Verify-AES-Key' + self.shared).digest()[:16]
            iv = hashlib.sha512(b'Pair-Verify-AES-IV' + self.shared).digest()[:16]
            self.verify_cipher = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
            signature = self.receiver.private.sign(self.public + self.peer_ecdh)
            return self.public + self.verify_cipher.update(signature)
        if len(body) == 68 and body[0] == 0 and self.shared:
            signature = self.verify_cipher.update(body[4:])
            try:
                Ed25519PublicKey.from_public_bytes(self.peer_ed).verify(signature, self.peer_ecdh + self.public)
            except Exception:
                self.shared = None; self.verified = False
                raise
            self.verified = True
            return b''
        raise ValueError('Invalid AirPlay pair-verify')

    def start_pin(self):
        # iOS closes the PIN-display socket and continues setup on a new TCP
        # connection. Keep the short-lived transaction at receiver scope.
        for peer,state in list(self.receiver.pin_sessions.items()):
            if time.monotonic()-state.pin_at > 180: self.receiver.pin_sessions.pop(peer,None)
        if len(self.receiver.pin_sessions) >= 4 and self.peer not in self.receiver.pin_sessions:
            raise ValueError('Too many AirPlay PIN transactions')
        self.pin = f'{secrets.randbelow(10000):04d}'
        self.pin_at = time.monotonic()
        self.srp = None; self.pin_verified = False
        self.receiver.pin_sessions[self.peer] = self
        self.runtime.callback.onAirPlayPin(self.pin)

    def pair_pin(self, request):
        transaction = self.receiver.pin_sessions.get(self.peer)
        if transaction is not None and transaction is not self:
            return transaction.pair_pin(request)
        if self.pin is None or time.monotonic()-self.pin_at > 180:
            self.receiver.pin_sessions.pop(self.peer,None)
            raise PermissionError('AirPlay PIN expired, please reconnect')
        if request.get('method') == 'pin' and isinstance(request.get('user'),str):
            if len(request['user']) > 64: raise ValueError('Invalid AirPlay pairing identity')
            self.pin_salt = secrets.token_bytes(16)
            while self.pin_salt[0] == 0: self.pin_salt = secrets.token_bytes(16)
            context = PinContext(request['user'],self.pin,prime=PRIME_2048,generator=PRIME_2048_GEN,
                                 hash_func=hashlib.sha1)
            verifier = context.get_common_password_verifier(context.get_common_password_hash(self.pin_salt))
            self.srp = SRPServerSession(context,format(verifier,'x'),private=secrets.token_bytes(32).hex())
            return {'salt':self.pin_salt,'pk':bytes.fromhex(self.srp.public)}
        if 'pk' in request and 'proof' in request and self.srp:
            if len(request['pk']) > 256 or len(request['proof']) != 20:
                raise ValueError('Invalid AirPlay PIN proof length')
            self.srp.process(request['pk'].hex(),self.pin_salt.hex())
            if not self.srp.verify_proof(request['proof'].hex().encode()):
                self.srp = None
                raise PermissionError('Incorrect AirPlay PIN')
            self.pin_verified = True
            return {'proof':bytes.fromhex(self.srp.key_proof_hash.decode())}
        if 'epk' in request and 'authTag' in request and self.srp and self.pin_verified:
            if len(request['epk']) != 32 or len(request['authTag']) != 16:
                raise ValueError('Invalid AirPlay PIN public key')
            secret = bytes.fromhex(self.srp.key.decode())
            key = hashlib.sha512(b'Pair-Setup-AES-Key'+secret).digest()[:16]
            iv = bytearray(hashlib.sha512(b'Pair-Setup-AES-IV'+secret).digest()[:16])
            iv[-1] = (iv[-1]+1)&255
            peer = AESGCM(key).decrypt(bytes(iv),request['epk']+request['authTag'],None)
            iv[-1] = (iv[-1]+1)&255
            encrypted = AESGCM(key).encrypt(bytes(iv),self.receiver.public,None)
            self.receiver.register_client(peer)
            self.srp = None; self.pin = None
            self.receiver.pin_sessions.pop(self.peer,None)
            self.runtime.callback.onAirPlayPin('')
            return {'epk':encrypted[:32],'authTag':encrypted[32:]}
        raise ValueError('Unexpected AirPlay PIN pairing stage')

    def fairplay(self, body):
        if not self.verified: raise PermissionError('AirPlay pairing required')
        if len(body) not in (16, 164) or body[:5] != b'FPLY\x03':
            raise ValueError('Invalid FairPlay request')
        if len(body) == 16:
            mode = body[14]
            if mode > 3: raise ValueError('Invalid FairPlay mode')
            self.keymsg = None
            table = Path(__file__).with_name('fairplay.bin').read_bytes()
            return table[mode*142:(mode+1)*142]
        # PlayFair indexes its tables by this message byte.
        if body[12] > 3: raise ValueError('Invalid FairPlay handshake mode')
        self.keymsg = body
        return bytes.fromhex('46504c590301040000000014') + body[144:164]

    async def timing_requests(self, port):
        while True:
            stamp = int((time.time()+2208988800) * 2**32)
            destination = (self.peer,port,0,self.scope) if ':' in self.peer else (self.peer,port)
            self.timing.sendto(b'\x80\xd2\0\x07' + b'\0'*20 + struct.pack('>Q', stamp), destination)
            await asyncio.sleep(3)

    async def event(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task); self.writers.add(writer)
        try:
            if peer_host(writer.get_extra_info('peername')[0]) != self.peer or len(self.writers) > 4: return
            while await reader.read(4096): pass
        finally:
            self.tasks.discard(task); self.writers.discard(writer); writer.close()

    async def setup(self, request):
        if not self.verified: raise PermissionError('AirPlay pairing required')
        result = {}
        if 'ekey' in request:
            if self.keymsg is None or len(request['ekey']) != 72:
                raise ValueError('Missing FairPlay stream key')
            key = self.receiver.unwrap(self.keymsg, request['ekey'])
            self.base_key = key
            self.key = hashlib.sha512(key + self.shared).digest()[:16]
            loop = asyncio.get_running_loop()
            if self.timing is None:
                bind = '::' if ':' in self.peer else '0.0.0.0'
                self.timing, _ = await loop.create_datagram_endpoint(Timing, local_addr=(bind, 0))
                self.event_server = await listen(self.event)
                port = int(request.get('timingPort', 0))
                if 0 < port <= 65535: self.timing_task = asyncio.create_task(self.timing_requests(port))
            result.update(timingPort=self.timing.get_extra_info('sockname')[1],
                          eventPort=self.event_server.sockets[0].getsockname()[1])
        streams = []
        for stream in request.get('streams', []):
            kind = stream['type']
            if kind == 110:
                if self.key is None: raise ValueError('Missing AirPlay key setup')
                if self.receiver.owner is not None and self.receiver.owner is not self:
                    raise ValueError('Another iPhone is already mirroring')
                if self.video_server is not None: raise ValueError('AirPlay stream already active')
                self.receiver.owner = self
                self.decryptor = mirror_cipher(self.key, stream['streamConnectionID'])
                self.legacy_decryptor = mirror_cipher(self.base_key, stream['streamConnectionID'])
                self.key_mode = None
                self.video_server = await listen(self.video)
                streams.append({'type': 110, 'dataPort': self.video_server.sockets[0].getsockname()[1]})
                peer_address = self.peer+(f'%{self.scope}' if ':' in self.peer and self.scope else '')
                self.runtime.set_peer(peer_address)
            elif kind == 96:
                # The screen sender may also open audio, which this build discards.
                if len(self.audio_sinks) >= 2: raise ValueError('Too many AirPlay audio streams')
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    asyncio.DatagramProtocol, local_addr=('::' if ':' in self.peer else '0.0.0.0', 0))
                self.receiver.audio_sinks.append(transport)
                self.audio_sinks.append(transport)
                port = transport.get_extra_info('sockname')[1]
                streams.append({'type': 96, 'dataPort': port, 'controlPort': port})
            else: raise ValueError('Unsupported AirPlay stream type')
        if streams: result['streams'] = streams
        return result

    async def video(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task); self.writers.add(writer)
        admitted = False
        decoder_worker = None
        packets = asyncio.Queue(maxsize=4)
        csd = None
        packet_kind = None
        packet_size = 0
        packet_flags = 0
        def report_error(error):
            self.runtime.status('video', '无线画面已中断，请重新选择屏幕镜像')
            self.runtime.callback.onDiagnostic(
                f'AirPlay video: {error}; kind={packet_kind} flags=0x{packet_flags:04x} '
                f'bytes={packet_size} configured={csd is not None}')
        async def decode_packets():
            try:
                while True:
                    args,source_us = await packets.get()
                    metric = getattr(self.runtime,'metric',None)
                    if metric: metric('video_queue',(time.monotonic_ns()//1000-args[-1])/1e6)
                    timed = getattr(self.runtime,'on_video_timed',None)
                    callback = timed or self.runtime.on_video
                    values = (*args,source_us) if timed else args
                    self.decode_task = asyncio.create_task(asyncio.to_thread(callback,*values))
                    await asyncio.shield(self.decode_task)
                    self.decode_task = None
            finally:
                # Do not detach an active JNI/codec callback on cancellation.
                if self.decode_task is not None:
                    with suppress(Exception,asyncio.CancelledError): await asyncio.shield(self.decode_task)
                    self.decode_task = None
        def decoder_finished(worker):
            if not worker.cancelled() and worker.exception() is not None:
                report_error(worker.exception())
                writer.close()
        try:
            if self.closed or peer_host(writer.get_extra_info('peername')[0]) != self.peer or self.video_writer: return
            self.video_writer = writer; admitted = True
            decoder_worker=asyncio.create_task(decode_packets())
            self.tasks.add(decoder_worker)
            decoder_worker.add_done_callback(decoder_finished)
            report_at=None
            wire_frames=source_steps=source_span_us=queue_peak=backpressure=0
            last_source_us=None
            while not self.runtime.stop.is_set():
                header = await asyncio.wait_for(reader.readexactly(128), 30)
                size, = struct.unpack_from('<I', header)
                packet_kind, packet_size = header[4], size
                packet_flags=struct.unpack_from('<H',header,6)[0]
                limit=video_packet_limit(width,height) if packet_kind==0 and csd is not None else MAX_BODY
                if size > limit: raise ValueError(f'AirPlay video packet too large (limit={limit})')
                payload = await asyncio.wait_for(reader.readexactly(size), 10)
                received_us = time.monotonic_ns()//1000
                if header[4] == 1:
                    csd, width, height, length_size = codec_packet(header, payload)
                    self.runtime.callback.onDiagnostic(f'AirPlay codec: {width}x{height} length_size={length_size} config_bytes={len(payload)}')
                elif header[4] == 0:
                    data = self.decryptor.update(payload)
                    if csd is None: raise ValueError('AirPlay frame before codec configuration')
                    if self.key_mode is None:
                        legacy = self.legacy_decryptor.update(payload)
                        try:
                            data = annex_b(data, 4)
                            self.key_mode = 'paired'
                        except ValueError as paired_error:
                            try: data = annex_b(legacy, 4)
                            except ValueError as legacy_error:
                                raise ValueError(f'首帧解密/分帧失败：paired={paired_error}; legacy={legacy_error}') from legacy_error
                            self.decryptor = self.legacy_decryptor
                            self.key_mode = 'legacy'
                        self.runtime.callback.onDiagnostic('AirPlay video key mode: '+self.key_mode)
                    else:
                        data = annex_b(data, 4)
                    source_raw=struct.unpack_from('<Q',header,8)[0]
                    source_us=source_raw*1_000_000>>32
                    now=time.monotonic()
                    if report_at is None: report_at=now
                    wire_frames+=1
                    if last_source_us is not None and source_us>last_source_us:
                        source_steps+=1; source_span_us+=source_us-last_source_us
                    last_source_us=source_us
                    sample=((data,csd,width,height,'video/avc',received_us),source_us)
                    # Receive/decrypt the next TCP packet while MediaCodec is
                    # busy. Bound the queue and preserve every reference frame;
                    # display backpressure is handled only after decoding.
                    if decoder_worker.done(): decoder_worker.result()
                    try: packets.put_nowait(sample)
                    except asyncio.QueueFull:
                        backpressure+=1
                        enqueue=asyncio.create_task(packets.put(sample))
                        try:
                            done,_=await asyncio.wait((enqueue,decoder_worker),return_when=asyncio.FIRST_COMPLETED)
                            if decoder_worker in done: decoder_worker.result()
                            await enqueue
                        finally:
                            if not enqueue.done():
                                enqueue.cancel()
                                await asyncio.gather(enqueue,return_exceptions=True)
                    queue_peak=max(queue_peak,packets.qsize())
                    if now-report_at>=5:
                        source_rate=f'{source_steps*1e6/source_span_us:.1f}' if source_span_us else 'unknown'
                        self.runtime.callback.onDiagnostic(
                            f'AirPlay performance: wireFps={wire_frames/(now-report_at):.1f} '
                            f'sourceFps={source_rate} queuePeak={queue_peak} backpressure={backpressure}')
                        report_at=now
                        wire_frames=source_steps=source_span_us=queue_peak=backpressure=0
        except asyncio.IncompleteReadError: pass
        except Exception as error:
            report_error(error)
        finally:
            if decoder_worker is not None:
                if not decoder_worker.cancelling(): decoder_worker.cancel()
                await asyncio.gather(decoder_worker,return_exceptions=True)
                self.tasks.discard(decoder_worker)
            self.tasks.discard(task); self.writers.discard(writer); writer.close()
            if admitted:
                self.video_writer = None
                self.runtime.clear_peer(self.peer)
                self.runtime.callback.onVideoDisconnected()
                await self.close()


class Receiver:
    def __init__(self, runtime, name, unwrap=unwrap_key):
        self.runtime, self.name = runtime, name
        self.unwrap = unwrap
        self.width,self.height,self.fps=getattr(runtime,'video_profile',(1920,1080,60))
        if not (320<=self.width<=8192 and 180<=self.height<=8192 and self.fps in (24,30,60,120)):
            raise ValueError('Invalid AirPlay display profile')
        self.private = identity(runtime.root)
        self.public = self.private.public_key().public_bytes_raw()
        digest = hashlib.sha256(self.public).digest()
        self.device_id = ':'.join(f'{b:02X}' for b in bytes([(digest[0] | 2) & 254]) + digest[1:6])
        self.pi = str(uuid.UUID(bytes=digest[:16]))
        self.sessions = set()
        self.tasks = set()
        self.writers = set()
        self.audio_sinks = []
        self.server = None
        self.owner = None
        self.pin_sessions = {}
        self.client_path = Path(runtime.root)/'airplay-clients.json'
        try: self.clients = set(json.loads(self.client_path.read_text()))
        except (OSError,ValueError): self.clients = set()

    def register_client(self, public):
        self.clients.add(public.hex())
        temporary = self.client_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(sorted(self.clients)))
        temporary.replace(self.client_path)

    def txt(self):
        return {'deviceid': self.device_id, 'features': f'0x{FEATURES:X},0x0', 'flags': '0x84',
                'model': 'AppleTV3,2', 'pk': self.public.hex(), 'pi': self.pi,
                'srcvers': '220.68', 'vv': '2'}

    def info(self):
        txt = b''.join(bytes([len(s)]) + s for s in (f'{k}={v}'.encode() for k,v in self.txt().items()))
        return dict(deviceID=self.device_id, macAddress=self.device_id, pk=self.public,
            features=FEATURES, name=self.name, pi=self.pi, vv=2, statusFlags=68,
            sourceVersion='220.68', model='AppleTV3,2', keepAliveLowPower=1,
            keepAliveSendStatsAsBody=True, initialVolume=0.0, txtAirPlay=txt,
            displays=[dict(uuid=self.pi, widthPhysical=0, heightPhysical=0, width=self.width,
                height=self.height, widthPixels=self.width, heightPixels=self.height, rotation=False,
                refreshRate=1/self.fps, maxFPS=self.fps, overscanned=False, features=14)],
            audioFormats=[dict(type=n, audioInputFormats=0x3fffffc, audioOutputFormats=0x3fffffc) for n in (100,101)],
            audioLatencies=[dict(type=n, audioType='default', inputLatencyMicros=0, outputLatencyMicros=0) for n in (100,101)])

    async def start(self):
        self.server = await listen(self.handle, limit=32768)
        port = self.server.sockets[0].getsockname()[1]
        self.runtime.callback.onAirPlayReady(port, self.name, json.dumps(self.txt()))
        self.runtime.callback.onDiagnostic(f'AirPlay requested display: {self.width}x{self.height}@{self.fps}')

    async def close(self):
        if self.server:
            self.server.close()
        for writer in list(self.writers): writer.close()
        for task in list(self.tasks): task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.server: await self.server.wait_closed()
        for transport in self.audio_sinks: transport.close()
        self.audio_sinks.clear()
        self.pin_sessions.clear()
        self.runtime.callback.onAirPlayPin('')

    async def handle(self, reader, writer):
        if len(self.sessions) >= 4:
            writer.close(); return
        task = asyncio.current_task()
        self.tasks.add(task); self.writers.add(writer)
        session = Session(self, writer.get_extra_info('peername'))
        self.sessions.add(session)
        self.runtime.callback.onDiagnostic('AirPlay client connected: '+session.peer)
        try:
            while not self.runtime.stop.is_set():
                raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 90)
                parts = raw.decode('iso-8859-1').split('\r\n')
                method, url, protocol = parts[0].split(' ')
                headers = dict(line.lower().split(':',1) for line in parts[1:] if ':' in line)
                headers = {k:v.strip() for k,v in headers.items()}
                size = int(headers.get('content-length', 0))
                if not 0 <= size <= MAX_BODY: raise ValueError('AirPlay request too large')
                body = await asyncio.wait_for(reader.readexactly(size), 15)
                data, content, code = b'', 'application/octet-stream', 200
                path = url.split('?',1)[0]
                try:
                    if method in ('GET','POST') and path == '/info':
                        data = plistlib.dumps(self.info(), fmt=plistlib.FMT_BINARY)
                        content = 'application/x-apple-binary-plist'
                    elif path == '/pair-setup':
                        if len(body) != 32: raise ValueError('Invalid pair-setup')
                        if body.hex() not in self.clients:
                            raise PermissionError('AirPlay PIN pairing required')
                        data = self.public
                    elif path == '/pair-pin-start': session.start_pin()
                    elif path == '/pair-setup-pin':
                        data = plistlib.dumps(session.pair_pin(plistlib.loads(body)),fmt=plistlib.FMT_BINARY)
                        content = 'application/x-apple-binary-plist'
                    elif path == '/pair-verify': data = session.pair_verify(body)
                    elif path == '/fp-setup': data = session.fairplay(body)
                    elif method == 'SETUP':
                        data = plistlib.dumps(await session.setup(plistlib.loads(body)), fmt=plistlib.FMT_BINARY)
                        content = 'application/x-apple-binary-plist'
                    elif method == 'TEARDOWN':
                        await session.close()
                    elif method == 'GET_PARAMETER':
                        if b'volume' in body: data = b'volume: 0.000000\r\n'
                        content = 'text/parameters'
                    elif method not in ('OPTIONS','RECORD','SET_PARAMETER','FLUSH') and path not in ('/feedback','/rate','/audioMode'):
                        code = 501
                except PermissionError as error:
                    code = 470 if path == '/pair-setup-pin' else 403
                    self.runtime.callback.onDiagnostic('AirPlay authentication: '+str(error))
                except Exception as error:
                    self.runtime.callback.onDiagnostic('AirPlay request '+method+' '+path+': '+str(error))
                    code = 400
                response = [f'{protocol} {code} '+('OK' if code==200 else 'Error'),
                    'Server: AirTunes/220.68', 'Content-Type: '+content, 'Content-Length: '+str(len(data))]
                if 'cseq' in headers: response.append('CSeq: '+headers['cseq'])
                if 'cseq' in headers and method != 'RECORD': response.append('Audio-Jack-Status: connected; type=digital')
                if method == 'OPTIONS': response.append('Public: OPTIONS, SETUP, RECORD, SET_PARAMETER, GET_PARAMETER, FLUSH, TEARDOWN')
                if method in ('SETUP','RECORD'): response.append('Session: 1')
                if path not in ('/feedback','/rate'):
                    self.runtime.callback.onDiagnostic(f'AirPlay handshake: {method} {path[:80]} bytes={len(body)} status={code}')
                writer.write(('\r\n'.join(response)+'\r\n\r\n').encode()+data)
                await writer.drain()
                if method == 'TEARDOWN': break
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError): pass
        except Exception as error: self.runtime.callback.onDiagnostic('AirPlay client: '+str(error))
        finally:
            await session.close()
            self.runtime.callback.onDiagnostic('AirPlay client closed: '+session.peer)
            self.sessions.discard(session); self.tasks.discard(task); self.writers.discard(writer)
            writer.close()
