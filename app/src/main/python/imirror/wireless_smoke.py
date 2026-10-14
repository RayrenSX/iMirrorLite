"""Synthetic AirPlay sender against the real receiver, including on Android.

This verifies encrypted packets and socket cleanup, not iPhone compatibility.
"""
import asyncio
import hashlib
import plistlib
import struct
import threading
import tempfile
from contextlib import suppress
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from imirror.airplay import Receiver, PinContext, mirror_cipher


async def probe_pin(root, address='127.0.0.1'):
    """Reproduce iOS opening a new socket for every PIN pairing stage."""
    from srptools import SRPClientSession
    from srptools.constants import PRIME_2048, PRIME_2048_GEN
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pins = []
    runtime = SimpleNamespace(root=str(root),stop=threading.Event(),
        callback=SimpleNamespace(onDiagnostic=lambda msg:None,onAirPlayReady=lambda *args:None,onAirPlayPin=pins.append))
    receiver = Receiver(runtime,'iMirror PIN Test')
    try:
        await receiver.start()
        async def request(path, body=b''):
            reader,writer = await asyncio.open_connection(address,receiver.server.sockets[0].getsockname()[1])
            try:
                writer.write(f'POST {path} RTSP/1.0\r\nCSeq: 1\r\nContent-Length: {len(body)}\r\n\r\n'.encode()+body)
                await writer.drain()
                header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
                assert b' 200 ' in header, header
                length = int(next(s.split(b':')[1] for s in header.split(b'\r\n') if s.startswith(b'Content-Length:')))
                return await reader.readexactly(length)
            finally:
                writer.close(); await writer.wait_closed()
        await request('/pair-pin-start')
        await asyncio.sleep(.01)  # Let the receiver observe the first socket FIN.
        reply = plistlib.loads(await request('/pair-setup-pin',plistlib.dumps({'method':'pin','user':'client'},fmt=plistlib.FMT_BINARY)))
        context = PinContext('client',pins[-1],prime=PRIME_2048,generator=PRIME_2048_GEN,hash_func=hashlib.sha1)
        client = SRPClientSession(context)
        client.process(reply['pk'].hex(),reply['salt'].hex())
        body = plistlib.dumps({'pk':bytes.fromhex(client.public),'proof':bytes.fromhex(client.key_proof.decode())},fmt=plistlib.FMT_BINARY)
        reply = plistlib.loads(await request('/pair-setup-pin',body))
        assert client.verify_proof(reply['proof'].hex().encode())
        key = bytes.fromhex(client.key.decode())
        aes = hashlib.sha512(b'Pair-Setup-AES-Key'+key).digest()[:16]
        iv = bytearray(hashlib.sha512(b'Pair-Setup-AES-IV'+key).digest()[:16]); iv[-1]=(iv[-1]+1)&255
        public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        encrypted = AESGCM(aes).encrypt(bytes(iv),public,None)
        reply = plistlib.loads(await request('/pair-setup-pin',plistlib.dumps({'epk':encrypted[:32],'authTag':encrypted[32:]},fmt=plistlib.FMT_BINARY)))
        iv[-1]=(iv[-1]+1)&255
        assert AESGCM(aes).decrypt(bytes(iv),reply['epk']+reply['authTag'],None) == receiver.public
        assert pins[-1] == '' and not receiver.pin_sessions
    finally:
        await receiver.close()


async def probe(root, unwrap, output=None, codec=None, access_unit=None, length_hint=4, legacy=False, timed_output=None):
    frames = []
    received = asyncio.Event()
    ready = []
    runtime = SimpleNamespace(root=str(root), stop=threading.Event(),
        set_peer=lambda host: None, clear_peer=lambda host: None,
        status=lambda *args: None,
        callback=SimpleNamespace(onAirPlayReady=lambda port,*args: ready.append(port),
            onDiagnostic=lambda text: None, onVideoDisconnected=lambda: None, onAirPlayPin=lambda pin:None))
    loop = asyncio.get_running_loop()
    def video(*args):
        frames.append(args)
        if output: output(*args)
        loop.call_soon_threadsafe(received.set)
    runtime.on_video = video
    if timed_output:
        def timed(data,csd,width,height,mime,pts,source_us):
            frames.append((data,csd,width,height,mime,pts))
            timed_output(data,csd,width,height,mime,pts,source_us)
            loop.call_soon_threadsafe(received.set)
        runtime.on_video_timed=timed
    receiver = Receiver(runtime, 'iMirror Test', unwrap)
    sender = None
    video_writer = None
    try:
        await receiver.start()
        reader, sender = await asyncio.open_connection('127.0.0.1', ready[0])
        seq = 0
        async def request(method, path, body=b''):
            nonlocal seq
            seq += 1
            sender.write(f'{method} {path} RTSP/1.0\r\nCSeq: {seq}\r\nContent-Length: {len(body)}\r\n\r\n'.encode()+body)
            await sender.drain()
            response = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            headers = dict(line.split(': ',1) for line in response.decode().split('\r\n')[1:] if ': ' in line)
            data = await reader.readexactly(int(headers['Content-Length']))
            if b' 200 ' not in response: raise AssertionError(response)
            return data
        info = plistlib.loads(await request('GET','/info'))
        assert info['features'] & (1<<7) and info['displays'][0]['maxFPS'] == 60
        host_ed = Ed25519PrivateKey.generate()
        host_x = X25519PrivateKey.generate()
        public_x = host_x.public_key().public_bytes_raw()
        public_ed = host_ed.public_key().public_bytes_raw()
        # This separate signed-video probe represents an already PIN-paired
        # sender; probe_pin exercises first-time registration across TCP.
        receiver.register_client(public_ed)
        assert await request('POST','/pair-setup',public_ed) == receiver.public
        reply = await request('POST','/pair-verify',b'\1\0\0\0'+public_x+public_ed)
        shared = host_x.exchange(X25519PublicKey.from_public_bytes(reply[:32]))
        key = hashlib.sha512(b'Pair-Verify-AES-Key'+shared).digest()[:16]
        iv = hashlib.sha512(b'Pair-Verify-AES-IV'+shared).digest()[:16]
        verification = Cipher(algorithms.AES(key),modes.CTR(iv)).encryptor()
        Ed25519PublicKey.from_public_bytes(receiver.public).verify(verification.update(reply[32:]), reply[:32]+public_x)
        signature = host_ed.sign(public_x+reply[:32])
        await request('POST','/pair-verify',b'\0'*4+verification.update(signature))
        first = bytearray(16); first[:5] = b'FPLY\3'
        assert len(await request('POST','/fp-setup',first)) == 142
        message = bytearray(164); message[:5] = b'FPLY\3'
        encrypted = bytes(72)
        assert len(await request('POST','/fp-setup',message)) == 32
        base_key = unwrap(bytes(message), encrypted)
        audio_key = base_key if legacy else hashlib.sha512(base_key+shared).digest()[:16]
        await request('SETUP','rtsp://localhost/stream',plistlib.dumps({'ekey': encrypted, 'eiv': bytes(16)},fmt=plistlib.FMT_BINARY))
        sid = 1234567890123
        setup = plistlib.loads(await request('SETUP','rtsp://localhost/stream',plistlib.dumps(
            {'streams':[{'type':110,'streamConnectionID':sid}]},fmt=plistlib.FMT_BINARY)))
        _, video_writer = await asyncio.open_connection('127.0.0.1',setup['streams'][0]['dataPort'])
        sps, pps = bytes.fromhex('6742001e95a814016e40'), bytes.fromhex('68ce06e2')
        if codec:
            units = [u for u in codec.split(b'\0\0\0\1') if u]
            sps = next(u for u in units if u[0]&31 == 7)
            pps = next(u for u in units if u[0]&31 == 8)
        avcc = bytes([1,66,0,30,252+(length_hint-1),225])+struct.pack('>H',len(sps))+sps+b'\1'+struct.pack('>H',len(pps))+pps
        stamp=0
        def packet(kind, data):
            nonlocal stamp
            header = bytearray(128); struct.pack_into('<I',header,0,len(data)); header[4]=kind
            struct.pack_into('<ff',header,56,640,960)
            struct.pack_into('<Q',header,8,((10_000_000+stamp*1_000_000//60)<<32)//1_000_000)
            if kind==0: stamp+=1
            return bytes(header)+data
        plaintext = b'\0\0\0\5\x65test'
        if access_unit:
            plaintext = b''.join(struct.pack('>I',len(u))+u for u in access_unit.split(b'\0\0\0\1') if u)
        encryptor = mirror_cipher(audio_key, sid)
        # Split packets and TCP writes at unrelated boundaries. CTR state spans
        # frames, including partial AES blocks.
        count = 12 if output else 2
        configuration=packet(1,avcc)
        encoded=[packet(0,encryptor.update(plaintext)) for _ in range(count)]
        # Rendering uses a real 60 fps sender cadence; emitting all twelve
        # frames at once only tests overload and hardware startup dropping.
        blocks=[configuration,*encoded] if output else [configuration+b''.join(encoded)]
        for block in blocks:
            for offset in range(0,len(block),17):
                video_writer.write(block[offset:offset+17]); await video_writer.drain()
            if output: await asyncio.sleep(1/60)
        await asyncio.wait_for(received.wait(),5)
        while len(frames)<count: await asyncio.sleep(.01)
        if not output: assert frames[0][0] == b'\0\0\0\1\x65test'
        assert frames[0][2:5] == (640,960,'video/avc')
        assert frames[1][0] == frames[0][0]
        await request('TEARDOWN','rtsp://localhost/stream')
        return ready[0]
    finally:
        for writer in (video_writer, sender):
            if writer:
                writer.close()
                with suppress(Exception): await writer.wait_closed()
        await receiver.close()
        assert not receiver.sessions and not receiver.tasks


def check(root):
    from java import jarray, jbyte, jclass
    from imirror import remote_pairing, wireless
    native = jclass('com.rayren.imirrorlite.AirPlayCrypto')
    import ssl
    assert hasattr(ssl.SSLContext,'set_psk_client_callback'), 'Missing TLS-PSK callback'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_ciphers('PSK')
    def unwrap(message, key): return bytes(native.unwrap(jarray(jbyte)(message),jarray(jbyte)(key)))
    async def bounded(scratch):
        port = await asyncio.wait_for(probe(scratch,unwrap),20)
        await asyncio.wait_for(probe_pin(scratch),15)
        await asyncio.wait_for(probe_pin(scratch,'::1'),15)
        await asyncio.wait_for(check_pairing_srp(),15)
        await asyncio.wait_for(check_tcp_tunnel(),20)
        return port
    with tempfile.TemporaryDirectory(prefix='wireless-smoke-',dir=str(root)) as scratch:
        port = asyncio.run(bounded(scratch))
    return 'wireless_ok; receiver_port='+str(port)+'; TLS-PSK available'


def render(root, callback, codec, frame):
    from java import jarray, jbyte, jclass
    native = jclass('com.rayren.imirrorlite.AirPlayCrypto')
    def unwrap(message,key): return bytes(native.unwrap(jarray(jbyte)(message),jarray(jbyte)(key)))
    def output(data,csd,w,h,mime,pts):
        callback.onWirelessVideo(jarray(jbyte)(data),jarray(jbyte)(csd),w,h,mime,pts)
    def timed(data,csd,w,h,mime,pts,source_us):
        callback.onWirelessVideoTimed(jarray(jbyte)(data),jarray(jbyte)(csd),w,h,mime,pts,source_us)
    async def bounded(scratch):
        await asyncio.wait_for(probe(scratch,unwrap,output,bytes(codec),bytes(frame),timed_output=timed),25)
    with tempfile.TemporaryDirectory(prefix='wireless-render-',dir=str(root)) as scratch:
        asyncio.run(bounded(scratch))
    return 'wireless_video_ok'


async def check_pairing_srp(bad_server_proof=False):
    """Exercise USB-prepared wireless pairing's actual SRP initialization/proof."""
    import hashlib
    from srptools import SRPContext, SRPServerSession
    from srptools.constants import PRIME_3072, PRIME_3072_GEN
    from imirror.remote_pairing import (RemotePairingTunnelService, PairConsentResult,
        PairingDataComponentTLVBuf, PairingDataComponentType as Type)
    context=SRPContext('Pair-Setup',password='000000',prime=PRIME_3072,
        generator=PRIME_3072_GEN,hash_func=hashlib.sha512)
    _,verifier,salt=context.get_user_data_triplet()
    server=SRPServerSession(context,verifier)
    pairing=RemotePairingTunnelService('smoke','127.0.0.1',1)
    pairing._init_srp_context(PairConsentResult(bytes.fromhex(server.public),bytes.fromhex(salt),None))
    async def respond(message):
        data=pairing.decode_tlv(PairingDataComponentTLVBuf.parse(message['data']))
        assert data[Type.STATE]==b'\x03'
        server.process(data[Type.PUBLIC_KEY].hex(),salt)
        assert server.verify_proof(data[Type.PROOF].hex().encode())
        proof=bytes(64) if bad_server_proof else bytes.fromhex(server.key_proof_hash.decode())
        return PairingDataComponentTLVBuf.build([
            {'type':Type.STATE,'data':b'\x04'}, {'type':Type.PROOF,'data':proof}])
    pairing._send_receive_pairing_data=respond
    await pairing._verify_proof()
    assert pairing.encryption_key==bytes.fromhex(server.key.decode())
    assert len(pairing.encryption_key)==64
    return 'remote_pairing_srp_ok'


async def check_tcp_tunnel():
    """Actual TLS-PSK socket + CoreDevice handshake + userspace IPv6 stack."""
    import json
    import ssl
    from imirror.wireless import tcp_provider
    from imirror.remote_pairing import RemotePairingProtocol
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
    psk = b'wireless-smoke-test-only-psk-12345'
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers('PSK')
    context.set_psk_server_callback(lambda identity: psk)
    finished = asyncio.Event()
    async def server_peer(reader,writer):
        try:
            head = await reader.readexactly(10)
            assert head[:8] == b'CDTunnel'
            body = json.loads(await reader.readexactly(struct.unpack('>H',head[8:])[0]))
            assert body['type'] == 'clientHandshakeRequest'
            reply = json.dumps({'clientParameters':{'address':'fd42::1','mtu':16000},
                'serverAddress':'fd42::2','serverRSDPort':58783}).encode()
            writer.write(b'CDTunnel'+struct.pack('>H',len(reply))+reply); await writer.drain()
            while await reader.read(4096): pass
        finally:
            writer.close(); finished.set()
    server = await asyncio.start_server(server_peer,'127.0.0.1',0,ssl=context)
    endpoint = await probe_control_discovery(server.sockets[0].getsockname()[1])
    async def listener(): return {'port':endpoint['port']}
    pairing = SimpleNamespace(hostname=endpoint['address'],encryption_key=psk,
        create_tcp_listener=listener,_set_psk_ciphers=RemotePairingProtocol._set_psk_ciphers)
    try:
        provider = await tcp_provider(pairing)
        async with provider.start_tcp_tunnel() as tunnel:
            assert tunnel.address == 'fd42::2' and tunnel.port == 58783
            async with UserspaceDialPlane(tunnel.client.tun,tunnel.address):
                assert not any(t.done() for t in tunnel.tasks)
        await asyncio.wait_for(finished.wait(),3)
    finally:
        server.close(); await server.wait_closed()


async def probe_control_discovery(port):
    """A local DNS-SD peer publishing IPv4 and IPv6 for the same SRV host."""
    import socket
    from pymobiledevice3.bonjour import _build_rr, _encode_srv, encode_name
    from imirror.discovery import resolve_peer
    from imirror.wireless import WirelessRuntime
    kind='_remotepairing._tcp.local.'
    instance='smoke.'+kind
    host='phone.local.'
    records=[_build_rr(kind,12,encode_name(instance),120,False),
        _build_rr(instance,33,_encode_srv(0,0,port,host),120,True),
        _build_rr(host,1,socket.inet_pton(socket.AF_INET,'127.0.0.1'),120,True),
        _build_rr(host,28,socket.inet_pton(socket.AF_INET6,'fe80::42'),120,True)]
    packet=struct.pack('!HHHHHH',0,0x8400,0,len(records),0,0)+b''.join(records)
    queried=asyncio.Event()
    closed=asyncio.get_running_loop().create_future()
    class Responder(asyncio.DatagramProtocol):
        def connection_made(self,transport): self.transport=transport
        def datagram_received(self,data,peer):
            assert peer[1]!=5353
            self.transport.sendto(packet,peer); queried.set()
        def connection_lost(self,error):
            if not closed.done(): closed.set_result(None)
    transport,_=await asyncio.get_running_loop().create_datagram_endpoint(Responder,local_addr=('127.0.0.1',0))
    try:
        endpoints=await resolve_peer('fe80::42%40',query_target=transport.get_extra_info('sockname'))
        assert queried.is_set() and len(endpoints)==1
        runtime=WirelessRuntime(SimpleNamespace(onDiagnostic=lambda msg:None,onStatus=lambda *args:None),'.','test')
        runtime.set_peer('fe80::42%40')
        endpoint=endpoints[0]
        runtime.endpoints[(endpoint['type'],endpoint['name'])]=endpoint
        selected=runtime.candidates()
        assert len(selected)==1 and selected[0]['address']=='127.0.0.1'
        print('WIRELESS_DISCOVERY_OK: IPv6 screen sender -> same DNS-SD host IPv4 -> TLS-PSK tunnel')
        return selected[0]
    finally:
        transport.close(); await asyncio.wait_for(closed,1)
