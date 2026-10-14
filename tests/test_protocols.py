import json
import struct
import sys
import threading
import unittest
import hashlib
import tempfile
from contextlib import nullcontext
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src/main/python'))
from imirror.quicktime import Framer, Session, chunk, tag, control, read_nal, parse_format, parameter_sets
from imirror.hid import Contacts, Encoder, parse_descriptor
from imirror.ddi import ticket_request, download_file, sign
from imirror.control import media_unsupported
from imirror.mux import (PacketAssembler, build_mux_packet, build_version_payload, MuxDevice,
                        MuxConnection, TcpSegment, MuxError, parse_tcp, build_tcp,
                        TH_ACK, TH_FIN, TH_RST, TX_WINDOW)

FIXTURES = Path(__file__).parent / 'fixtures'


def parallel_descriptor(count):
    # Digitizers/Touchscreen; report 9; parallel Finger collections.
    result = bytes.fromhex('05 0d 09 04 a1 01 85 09')
    for _ in range(count):
        result += bytes.fromhex('05 0d 09 22 a1 02 09 42 15 00 25 01 75 01 95 01 81 02'
                                '75 07 81 03 09 51 15 00 25 7f 75 08 81 02'
                                '05 01 09 30 09 31 15 00 26 ff 7f 75 10 95 02 81 02 c0')
    return result + bytes.fromhex('05 0d 09 54 15 00 25 05 75 08 95 01 81 02 c0')


class QuickTimeTests(unittest.TestCase):
    def test_shutdown_acknowledges_stop_and_drains_release_before_final_hpd0(self):
        writes, frames, errors = [], [], []
        requested = threading.Event()
        def write(data):
            writes.append(data)
            if data[16:20] == tag('hpd0'):
                requested.set()
        session = Session(write, lambda *args: frames.append(args))
        session.feed((FIXTURES / 'cvrp-request').read_bytes())
        writes.clear()
        def stop():
            try: session.stop()
            except Exception as error: errors.append(error)
        worker = threading.Thread(target=stop)
        worker.start()
        try:
            self.assertTrue(requested.wait(1))
            # Queued media and PING must not request more frames or reopen the
            # session while the phone is processing the release request.
            session.feed((FIXTURES / 'asyn-feed').read_bytes())
            session.feed(chunk('ping', b'\0' * 8))
            correlation = b'12345678'
            stop_packet = chunk('sync', struct.pack('<Q', 1) + tag('stop') + correlation)
            session.feed(stop_packet + control('rels'))
            self.assertEqual(writes, [control('hpd0'), chunk('rply', correlation + b'\0' * 8)])
            self.assertFalse(frames)
            self.assertTrue(worker.is_alive())
            release = control('rels')
            session.feed(release[:7])
            session.feed(release[7:])
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(writes[-1], control('hpd0'))
            session.stop()
            self.assertEqual(len(writes), 3)
        finally:
            session.released.set()
            worker.join(1)

    def test_failed_stop_write_is_not_replayed(self):
        writes = []
        def fail(data):
            writes.append(data)
            raise OSError('USB disconnected')
        session = Session(fail, lambda *args: None)
        with self.assertRaises(OSError): session.stop()
        session.stop()
        self.assertEqual(writes, [control('hpd0')])

    def test_captured_handshake_and_video(self):
        writes, videos = [], []
        session = Session(writes.append, lambda *args: videos.append(args))
        for name in ('cwpa-request1', 'afmt-request', 'cvrp-request', 'clok-request', 'time-request1'):
            data = (FIXTURES / name).read_bytes()
            # Actual USB transfers can split anywhere, including inside a header.
            for i in range(0, len(data), 7): session.feed(data[i:i + 7])
        self.assertIn((FIXTURES / 'afmt-reply').read_bytes(), writes)
        self.assertEqual(writes[0][16:20], tag('hpd1'))
        self.assertGreater(session.video_clock, 0)
        session.feed((FIXTURES / 'asyn-feed').read_bytes())
        self.assertEqual(writes[-1][16:20], tag('need'))

    def test_packet_lengths_and_nal_validation(self):
        for size in (0, 7, 32 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError): list(Framer().feed(struct.pack('<I', size)))
        for value in (b'\0\0', b'\0\0\0\x09abc', b'\0\0\0\0'):
            with self.assertRaises(ValueError): read_nal(value, 0, 4)

    def test_back_to_back_and_fragmented_packets(self):
        packets = [chunk('ping', b'abcd'), chunk('rply', b'1234')]
        f = Framer(); decoded = []
        for byte in b''.join(packets): decoded.extend(f.feed(bytes([byte])))
        self.assertEqual(decoded, [p[4:] for p in packets])

    def test_format_and_length_prefixed_frame_reach_decoder(self):
        sps, pps, idr = b'\x67\x42\0\x1e', b'\x68\xce\x06', b'\x65\x88\x84'
        avcc = (b'\x01\x42\0\x1e\xff\xe1' + len(sps).to_bytes(2, 'big') + sps
                + b'\x01' + len(pps).to_bytes(2, 'big') + pps)
        description = (chunk('mdia', tag('vide')) + chunk('vdim', struct.pack('<II', 1080, 1920))
                       + chunk('codc', tag('avc1')) + chunk('extn', chunk('datv', avcc)))
        sample = chunk('sbuf', chunk('fdsc', description) + chunk('nsmp', struct.pack('<I', 1))
                       + chunk('sdat', len(idr).to_bytes(4, 'big') + idr))
        frames = []
        writes = []
        def display(*args):
            # The phone can encode the next sample while Android converts and
            # decodes this one; NEED must already be on the USB transport.
            self.assertEqual(writes, [control('need', 55)])
            frames.append(args)
        session = Session(writes.append, display)
        session.video_clock = 55
        session.feed(chunk('asyn', struct.pack('<Q', 1) + tag('feed') + sample))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], b'\0\0\0\1' + idr)
        self.assertEqual(frames[0][1], b'\0\0\0\1' + sps + b'\0\0\0\1' + pps)
        self.assertEqual(frames[0][2:5], (1080, 1920, 'video/avc'))


class DownloadFallbackTests(unittest.TestCase):
    def test_tss_uses_published_apple_root_and_checks_response(self):
        import plistlib
        import ssl
        from types import SimpleNamespace
        payload = b'STATUS=0&MESSAGE=SUCCESS&REQUEST_STRING=' + plistlib.dumps({'ApImg4Ticket': b'test-ticket'})
        response = SimpleNamespace(raise_for_status=lambda: None, content=payload)
        with tempfile.TemporaryDirectory() as root, patch('requests.post', return_value=response) as send:
            self.assertEqual(sign({'ApECID': 1}, root), b'test-ticket')
            ca = send.call_args.kwargs['verify']
            self.assertTrue(Path(ca).is_file())
            context = ssl.create_default_context(cafile=ca)
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(send.call_args.args[0], 'https://gs.apple.com/TSS/controller?action=2')

    def entry(self, data):
        return {'size': len(data), 'sha': hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()}

    def response(self, blocks):
        from types import SimpleNamespace
        return nullcontext(SimpleNamespace(raise_for_status=lambda: None, iter_content=lambda size: iter(blocks)))

    def test_interrupted_raw_download_restarts_verified_blob_via_api(self):
        import requests
        data = b'correct developer image bytes'
        def interrupted():
            yield data[:8]
            raise requests.ConnectionError('reset')
        with tempfile.TemporaryDirectory() as root, patch('requests.get', side_effect=[self.response(interrupted()), self.response([data])]) as fetch:
            result = download_file(Path(root), 'Image.dmg', self.entry(data), 'fixed-commit', lambda message: None, lambda: False)
            self.assertEqual((Path(root)/'Image.dmg').read_bytes(), data)
            self.assertEqual(result, hashlib.sha256(data).hexdigest())
            self.assertIn('/git/blobs/'+self.entry(data)['sha'], fetch.call_args.args[0])
            self.assertEqual(fetch.call_args.kwargs['headers']['Accept'], 'application/vnd.github.raw+json')

    def test_invalid_content_never_becomes_a_cached_image(self):
        data = b'correct'
        with tempfile.TemporaryDirectory() as root, patch('requests.get', return_value=self.response([b'incorrect'])) as fetch:
            with self.assertRaises(ValueError):
                download_file(Path(root), 'Image.dmg', self.entry(data), 'fixed', lambda message: None, lambda: False)
            self.assertFalse((Path(root)/'Image.dmg').exists())
            self.assertEqual(fetch.call_count, 1)

    def test_cancelled_download_does_not_open_another_connection(self):
        with tempfile.TemporaryDirectory() as root, patch('requests.get') as fetch:
            with self.assertRaises(RuntimeError):
                download_file(Path(root), 'Image.dmg', self.entry(b'x'), 'fixed', lambda message: None, lambda: True)
            fetch.assert_not_called()


class TouchTests(unittest.TestCase):
    def point(self, ident, action, x=.2, y=.7): return dict(id=ident, action=action, x=x, y=y)

    def test_five_contacts_have_distinct_wire_identifiers(self):
        encoder = Encoder({'HIDReportDescriptor': parallel_descriptor(5)})
        self.assertEqual(encoder.capacity, 5)
        contacts = Contacts(encoder.capacity)
        frame = contacts.apply([self.point(i + 20, 'down', i / 5, .5) for i in range(5)])
        report = encoder.encode(frame)
        self.assertEqual(report[0], 9)
        self.assertEqual([report[2 + 6 * i] for i in range(5)], list(range(5)))
        self.assertEqual([report[1 + 6 * i] for i in range(5)], [1] * 5)
        self.assertEqual(report[-1], 5)
        frame = contacts.apply([self.point(22, 'up')])
        self.assertEqual(encoder.encode(frame)[1 + 12], 0)
        self.assertEqual(encoder.encode(frame)[-1], 4)
        frame = contacts.apply([self.point(99, 'down')])
        self.assertEqual(contacts.active[99]['slot'], 2)
        released = contacts.cancel()
        self.assertEqual(encoder.encode(released)[-1], 0)
        self.assertFalse(contacts.active)

    def test_reorder_does_not_change_slot(self):
        contacts = Contacts(5)
        contacts.apply([self.point(15, 'down'), self.point(2, 'down')])
        contacts.apply([self.point(2, 'move'), self.point(15, 'move')])
        self.assertEqual(contacts.active[15]['slot'], 0)
        self.assertEqual(contacts.active[2]['slot'], 1)
        contacts.apply([self.point(7, 'move')])
        self.assertNotIn(7, contacts.active)

    def test_bad_batch_is_atomic(self):
        contacts = Contacts(5)
        with self.assertRaises(ValueError):
            contacts.apply([self.point(0, 'down'), self.point(1, 'down', float('nan'))])
        self.assertFalse(contacts.active)

    def test_descriptor_absence_never_claims_multitouch(self):
        encoder = Encoder({})
        self.assertEqual(encoder.capacity, 1)
        self.assertEqual(len(encoder.encode([])), 58)
        self.assertEqual(encoder.encode([])[3], 2)
        with self.assertRaises(ValueError): parse_descriptor(bytes.fromhex('05'))
        with self.assertRaises(ValueError): parse_descriptor(bytes.fromhex('c0'))

    def test_swift_wrapped_report_descriptor(self):
        surface = {'_ServiceID': 257, '_CoreDevice_codablePropertyStorage': {
            'ReportDescriptor': {'data': parallel_descriptor(5)}}}
        self.assertEqual(Encoder(surface).capacity, 5)

    def test_unusable_contact_ids_do_not_advertise_five_fingers(self):
        descriptor = parallel_descriptor(5).replace(bytes.fromhex('09 51 15 00 25 7f'),
                                                   bytes.fromhex('09 51 15 00 25 00'))
        self.assertEqual(Encoder({'ReportDescriptor': descriptor}).capacity, 1)
        with self.assertRaises(ValueError):
            parse_descriptor(bytes.fromhex('75 00 97 ff ff ff 7f 81 02'))

    def test_different_logical_minimums_do_not_duplicate_contact_ids(self):
        descriptor = parallel_descriptor(2).replace(bytes.fromhex('09 51 15 00 25 7f'),
                                                   bytes.fromhex('09 51 15 01 25 7f'), 1)
        encoder = Encoder({'ReportDescriptor': descriptor})
        self.assertEqual(encoder.capacity, 2)
        contacts = Contacts(2)
        report = encoder.encode(contacts.apply([self.point(4, 'down'), self.point(9, 'down')]))
        self.assertEqual([report[2], report[8]], [1, 2])

    def test_descriptor_must_allow_release_and_zero_contact_count(self):
        descriptor = parallel_descriptor(5)
        for old, new in [('09 54 15 00', '09 54 15 01'), ('09 42 15 00', '09 42 15 01'),
                         ('09 54 15 00 25 05 75 08', '09 54 15 00 25 05 75 01')]:
            invalid = descriptor.replace(bytes.fromhex(old), bytes.fromhex(new))
            self.assertEqual(Encoder({'ReportDescriptor': invalid}).capacity, 1)

    def test_media_fallback_requires_explicit_9021_response(self):
        self.assertTrue(media_unsupported(RuntimeError(
            "Failed to invoke: com.apple.coredevice.feature.startmediastream. Got error: {'code': 9021}")))
        for error in (TimeoutError(), ConnectionError('port 9021'),
                      RuntimeError("startmediastream {'code': 90210}"),
                      RuntimeError("startmediastream {'code': 5}")):
            self.assertFalse(media_unsupported(error))


class UsbMuxTests(unittest.TestCase):
    def connected(self):
        mux = MuxDevice(lambda packet: None)
        conn = MuxConnection(mux, 1, 62078)
        conn.state = 'connected'
        conn.tx_ack = 100
        mux._conns[1] = conn
        return mux, conn

    def test_fin_keeps_final_response_and_reclaims_connection(self):
        mux, conn = self.connected()
        conn._on_segment(TcpSegment(62078, 1, 100, 0, TH_ACK | TH_FIN, 65536, b'final response'))
        self.assertTrue(conn.closed)
        self.assertNotIn(1, mux._conns)
        self.assertEqual(conn.recv(), b'final response')
        self.assertEqual(conn.recv(), b'')

    def test_rst_reclaims_connection(self):
        mux, conn = self.connected()
        conn._on_segment(TcpSegment(62078, 1, 100, 0, TH_RST, 65536, b''))
        self.assertTrue(conn.closed)
        self.assertNotIn(1, mux._conns)

    def test_shutdown_rejects_late_connections(self):
        mux, conn = self.connected()
        mux.close_all()
        with self.assertRaisesRegex(MuxError, 'session closed'):
            mux.connect(62078, timeout=0.1)
        self.assertTrue(conn.closed)
        self.assertFalse(mux._conns)

    def test_failed_syn_does_not_leave_a_connection(self):
        def unavailable(packet):
            raise OSError('device disconnected')
        mux = MuxDevice(unavailable)
        mux.ready.set()
        with self.assertRaises(OSError): mux.connect(62078)
        self.assertFalse(mux._conns)

    def test_delayed_close_does_not_remove_a_reused_port(self):
        mux, previous = self.connected()
        previous.close()
        current = MuxConnection(mux, previous.sport, 62078)
        mux._conns[current.sport] = current
        previous.close()
        self.assertIs(mux._conns[current.sport], current)

    def test_receive_window_reopens_and_overlap_is_not_duplicated(self):
        _, conn = self.connected()
        conn._on_segment(TcpSegment(62078, 1, 103, 0, TH_ACK, 65536, b'def'))
        conn._on_segment(TcpSegment(62078, 1, 100, 0, TH_ACK, 65536, b'abcde'))
        self.assertEqual(conn.recv(), b'abcdef')
        self.assertEqual(conn.tx_win, TX_WINDOW)
        conn._on_segment(TcpSegment(62078, 1, 106, 0, TH_ACK, 65536, b'x' * TX_WINDOW))
        self.assertEqual(conn.tx_win, 0)
        self.assertEqual(len(conn.recv(512)), 512)
        self.assertEqual(conn.tx_win, 512)

    def test_malformed_tcp_and_receive_overflow_fail_explicitly(self):
        data = bytearray(build_tcp(1, 2, 0, 0, TH_ACK, 65536))
        data[12] = 0x10
        with self.assertRaises(MuxError): parse_tcp(data)
        data[12] = 0xf0
        with self.assertRaises(MuxError): parse_tcp(data)
        _, conn = self.connected()
        with self.assertRaises(MuxError):
            conn._on_segment(TcpSegment(62078, 1, 100, 0, TH_ACK, 65536, b'x' * (TX_WINDOW + 1)))

    def test_version_handshake_and_zero_length_packet(self):
        writes = []
        mux = MuxDevice(writes.append, wmax_packet=20)
        mux.start()
        self.assertEqual(len(writes[0]), 20)
        self.assertEqual(writes[1], b'')
        mux.feed(build_mux_packet(1, 0, build_version_payload(2, 0)))
        self.assertTrue(mux.ready.is_set())
        self.assertEqual(struct.unpack('!I', writes[2][:4])[0], 2)
        self.assertEqual(writes[2][-1], 7)

    def test_repeated_version_reply_keeps_v1_framing(self):
        writes = []
        mux = MuxDevice(writes.append)
        reply = build_mux_packet(1, 0, build_version_payload(2, 0))
        mux.feed(reply)
        mux.feed(reply)
        self.assertEqual(mux.version, 2)
        self.assertTrue(mux.ready.is_set())
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(p[-1] == 7 for p in writes))

    def test_observed_device_magic_is_valid_without_changing_host_magic(self):
        writes = []
        mux = MuxDevice(writes.append)
        mux.feed(build_mux_packet(1, 0, build_version_payload(2, 0)))
        conn = MuxConnection(mux, 1, 62078)
        mux._conns[1] = conn
        reply = bytearray(build_mux_packet(2, 6, build_tcp(62078, 1, 0, 1, 0x12, 65536)))
        reply[8:12] = bytes.fromhex('faceface')
        mux.feed(reply)
        self.assertEqual(conn.state, 'connected')
        self.assertEqual(writes[-1][8:12], bytes.fromhex('feedface'))
        reply[8:12] = bytes.fromhex('bad0face')
        with self.assertRaisesRegex(MuxError, 'invalid mux magic'): mux.feed(reply)

    def test_corrupt_lengths_rejected(self):
        with self.assertRaises(Exception): PacketAssembler().feed(struct.pack('!II', 0, 7))


class DeveloperImageTests(unittest.TestCase):
    def test_personalization_selects_correct_chip_and_applies_rules(self):
        manifest = {'BuildIdentities': [{'ApBoardID': '0x2', 'ApChipID': '0x10', 'Manifest': {
            'LoadableTrustCache': {'Trusted': True, 'Info': {'RestoreRequestRules': [{
                'Conditions': {'ApRequiresImage4': True}, 'Actions': {'EPRO': True}}]}},
            'Untrusted': {'Trusted': False, 'Info': {}}}}]}
        request = ticket_request(manifest, {'BoardId': 2, 'ChipID': 16}, 123, b'nonce')
        self.assertNotIn('Untrusted', request)
        self.assertTrue(request['LoadableTrustCache']['EPRO'])
        self.assertEqual(request['LoadableTrustCache']['Digest'], b'')
        self.assertEqual(request['ApNonce'], b'nonce')


if __name__ == '__main__': unittest.main()
