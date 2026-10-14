import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/src/main/python'))
from imirror.airplay import Receiver, Session, PinContext, annex_b, codec_packet, mirror_cipher
from imirror.wireless import WirelessRuntime, normalized_host
from imirror.wireless_smoke import probe, probe_pin, check_tcp_tunnel, check_pairing_srp


class AirPlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_pair_verify_encrypted_fragmented_stream_and_teardown(self):
        # Desktop has no JNI; Android runtime check repeats with native PlayFair.
        with tempfile.TemporaryDirectory() as root:
            port = await asyncio.wait_for(probe(root,lambda message,key: bytes(range(16))),15)
        with self.assertRaises(OSError): await asyncio.open_connection('127.0.0.1',port)

    async def test_airplay_wire_nal_lengths_are_four_bytes_despite_config_hint(self):
        with tempfile.TemporaryDirectory() as root:
            await asyncio.wait_for(probe(root,lambda message,key: bytes(range(16)),length_hint=1),15)

    async def test_legacy_video_key_does_not_mix_pair_verify_secret(self):
        with tempfile.TemporaryDirectory() as root:
            await asyncio.wait_for(probe(root,lambda message,key: bytes(range(16)),legacy=True),15)

    async def test_repeated_start_close_releases_sockets(self):
        with tempfile.TemporaryDirectory() as root:
            for _ in range(3):
                await asyncio.wait_for(probe(root,lambda message,key: bytes(16)),15)

    async def test_ios_pin_flow_reopens_tcp_for_each_stage(self):
        with tempfile.TemporaryDirectory() as root:
            await asyncio.wait_for(probe_pin(root),15)
            await asyncio.wait_for(probe_pin(root,'::1'),15)

    async def test_authenticated_tls_psk_tunnel_and_userspace_network(self):
        await asyncio.wait_for(check_tcp_tunnel(),20)

    async def test_wireless_pairing_srp_setup_and_proof(self):
        self.assertEqual(await check_pairing_srp(),'remote_pairing_srp_ok')

    async def test_wireless_pairing_rejects_invalid_srp_server_proof(self):
        from pymobiledevice3.exceptions import PairingError
        with self.assertRaises(PairingError): await check_pairing_srp(bad_server_proof=True)

    async def test_remote_pairing_encrypted_request_and_response(self):
        import json
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from imirror.remote_pairing import RemotePairingTunnelService
        pairing=RemotePairingTunnelService('test','127.0.0.1',1)
        pairing.encryption_key=bytes(range(32))
        pairing._init_client_server_main_encryption_keys()
        async def respond(message):
            nonce=bytes(12)
            encrypted=message['message']['streamEncrypted']['_0']
            data=json.loads(pairing.client_cip.decrypt(nonce,encrypted,b''))
            self.assertEqual(data['request']['_0']['createListener']['transportProtocolType'],'tcp')
            result=json.dumps({'response':{'_1':{'createListener':{'port':5555}}}}).encode()
            import base64
            return {'message':{'streamEncrypted':{'_0':base64.b64encode(pairing.server_cip.encrypt(nonce,result,b'')).decode()}}}
        pairing.send_receive_request=respond
        self.assertEqual(await pairing.create_tcp_listener(),{'port':5555})
        self.assertEqual(pairing._encrypted_sequence_number,1)

    async def test_video_key_setup_requires_verified_peer(self):
        receiver = SimpleNamespace(runtime=SimpleNamespace())
        session = Session(receiver,'127.0.0.1')
        with self.assertRaises(PermissionError): await session.setup({'ekey':bytes(72)})
        with self.assertRaises(PermissionError): session.fairplay(bytes(16))

    async def test_only_screen_sender_is_control_candidate_and_lost_services_removed(self):
        runtime = WirelessRuntime(SimpleNamespace(onStatus=lambda *args:None, onDiagnostic=lambda text:None),'.','test')
        runtime.loop=asyncio.get_running_loop()
        runtime.device('{"name":"one","type":"_remotepairing._tcp","host":"192.168.1.2","port":123}')
        await asyncio.sleep(0)
        self.assertEqual(runtime.endpoints[('_remotepairing._tcp.','one')]['type'],'_remotepairing._tcp.')
        runtime.set_peer('192.168.1.3')
        self.assertFalse(any(e['host']==runtime.peer for e in runtime.endpoints.values()))
        runtime.device('{"name":"one","type":"_remotepairing._tcp.","host":"192.168.1.2","port":123,"lost":true}')
        await asyncio.sleep(0)
        self.assertFalse(runtime.endpoints)
        runtime.first_video_at=1
        runtime.clear_peer('192.168.1.3')
        self.assertIsNone(runtime.first_video_at)


class WireValidationTests(unittest.TestCase):
    def test_selected_video_profile_is_advertised_before_stream_setup(self):
        for width,height,fps in ((960,540,24),(1280,720,30),(1920,1080,60),(3840,2160,120)):
            with tempfile.TemporaryDirectory() as root:
                runtime=SimpleNamespace(root=root,video_profile=(width,height,fps))
                info=Receiver(runtime,'profile').info()
                display=info['displays'][0]
                self.assertEqual((display['width'],display['height']),(width,height))
                self.assertEqual((display['widthPixels'],display['heightPixels']),(width,height))
                self.assertEqual(display['maxFPS'],fps)
                self.assertEqual(display['refreshRate'],1/fps)

    def test_invalid_video_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for profile in ((0,1080,60),(1920,0,60),(1920,1080,0),(9000,1080,60)):
                with self.assertRaises(ValueError): Receiver(SimpleNamespace(root=root,video_profile=profile),'invalid')

    def test_pin_pairing_srp_and_aes_gcm_key_exchange(self):
        from srptools import SRPClientSession
        from srptools.constants import PRIME_2048,PRIME_2048_GEN
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        with tempfile.TemporaryDirectory() as root:
            pins=[]
            runtime=SimpleNamespace(root=root,callback=SimpleNamespace(onAirPlayPin=pins.append))
            receiver=Receiver(runtime,'test')
            session=Session(receiver,'127.0.0.1');session.start_pin()
            # Each iOS PIN stage can arrive on a different TCP session.
            reply=Session(receiver,'127.0.0.1').pair_pin({'method':'pin','user':'client'})
            context=PinContext('client',pins[-1],prime=PRIME_2048,generator=PRIME_2048_GEN,hash_func=hashlib.sha1)
            client=SRPClientSession(context);client.process(reply['pk'].hex(),reply['salt'].hex())
            reply=Session(receiver,'127.0.0.1').pair_pin({'pk':bytes.fromhex(client.public),'proof':bytes.fromhex(client.key_proof.decode())})
            self.assertTrue(client.verify_proof(reply['proof'].hex().encode()))
            secret=bytes.fromhex(client.key.decode());self.assertEqual(len(secret),40)
            key=hashlib.sha512(b'Pair-Setup-AES-Key'+secret).digest()[:16]
            iv=bytearray(hashlib.sha512(b'Pair-Setup-AES-IV'+secret).digest()[:16]);iv[-1]=(iv[-1]+1)&255
            public=Ed25519PrivateKey.generate().public_key().public_bytes_raw()
            encrypted=AESGCM(key).encrypt(bytes(iv),public,None)
            reply=Session(receiver,'127.0.0.1').pair_pin({'epk':encrypted[:32],'authTag':encrypted[32:]})
            iv[-1]=(iv[-1]+1)&255
            self.assertEqual(AESGCM(key).decrypt(bytes(iv),reply['epk']+reply['authTag'],None),receiver.public)
            self.assertIn(public.hex(),receiver.clients)
            self.assertEqual(pins[-1],'')
            self.assertFalse(receiver.pin_sessions)

    def test_pin_transaction_isolated_by_sender_and_expires(self):
        with tempfile.TemporaryDirectory() as root:
            receiver=Receiver(SimpleNamespace(root=root,callback=SimpleNamespace(onAirPlayPin=lambda pin:None)),'test')
            session=Session(receiver,'192.168.1.2');session.start_pin()
            with self.assertRaises(PermissionError):
                Session(receiver,'192.168.1.3').pair_pin({'method':'pin','user':'client'})
            self.assertIn('192.168.1.2',receiver.pin_sessions)
            session.pin_at-=181
            with self.assertRaises(PermissionError):
                Session(receiver,'192.168.1.2').pair_pin({'method':'pin','user':'client'})
            self.assertFalse(receiver.pin_sessions)

    def test_incorrect_pin_proof_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            receiver=Receiver(SimpleNamespace(root=root,callback=SimpleNamespace(onAirPlayPin=lambda pin:None)),'test')
            session=Session(receiver,'127.0.0.1');session.start_pin()
            session.pair_pin({'method':'pin','user':'client'})
            with self.assertRaises(PermissionError): session.pair_pin({'pk':b'\x02','proof':bytes(20)})

    def test_bad_nal_size_and_dimensions_rejected(self):
        for data in (b'\0\0\0\0',b'\0\0\0\5\x65',b'\0'):
            with self.assertRaises(ValueError): annex_b(data)
        with self.assertRaises(ValueError): codec_packet(bytes(128),bytes(7))

    def test_ctr_partial_blocks_continue_across_frames(self):
        key=bytes(16); whole=mirror_cipher(key,123).update(bytes(101))
        cipher=mirror_cipher(key,123)
        self.assertEqual(cipher.update(bytes(3))+cipher.update(bytes(11))+cipher.update(bytes(87)),whole)

    def test_mapped_ipv4_address_matches_sender(self):
        self.assertEqual(normalized_host('::ffff:192.168.1.2'),'192.168.1.2')
