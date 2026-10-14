"""QuickTime USB framing and H.264/HEVC CoreMedia parsing.

Ported from iPhoneMirror Core (GPL-3.0): QuickTimePacket, QuickTimeSession,
CoreMedia. All packet and nested chunk lengths are validated before decoding.
"""
import struct
import threading
import time

MAX_PACKET = 32 * 1024 * 1024


def tag(name):
    return name.encode('ascii')[::-1]


def chunk(name, data=b''):
    return struct.pack('<I', len(data) + 8) + tag(name) + data


def dictionary(values):
    pairs = []
    for key, value in values.items():
        if isinstance(value, bool):
            encoded = chunk('bulv', bytes([value]))
        elif isinstance(value, float):
            encoded = chunk('nmbv', b'\x06' + struct.pack('<d', value))
        elif isinstance(value, int):
            encoded = chunk('nmbv', b'\x03' + struct.pack('<I', value))
        elif isinstance(value, dict):
            encoded = dictionary(value)
        else:
            raise ValueError('Unsupported dictionary value')
        pairs.append(chunk('keyv', chunk('strk', key.encode()) + encoded))
    return chunk('dict', b''.join(pairs))


def control(name, clock=1, body=b''):
    return chunk('asyn', struct.pack('<Q', clock) + tag(name) + body)


def chunks(data):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('Truncated CoreMedia chunk')
        size = struct.unpack_from('<I', data, offset)[0]
        if size < 8 or size > len(data) - offset:
            raise ValueError('Invalid CoreMedia chunk length')
        yield data[offset + 4:offset + 8][::-1].decode('ascii'), data[offset + 8:offset + size]
        offset += size


class Framer:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 4:
            size = struct.unpack_from('<I', self.buffer)[0]
            if not 8 <= size <= MAX_PACKET:
                self.buffer.clear()
                raise ValueError('Invalid QuickTime packet length')
            if len(self.buffer) < size:
                break
            packet = bytes(self.buffer[4:size])
            del self.buffer[:size]
            yield packet


def parameter_sets(config, hevc):
    sets = []
    if not config or config[0] != 1:
        raise ValueError('Invalid decoder configuration')
    if hevc:
        if len(config) < 23:
            raise ValueError('Short hvcC')
        length_size, offset = (config[21] & 3) + 1, 23
        for _ in range(config[22]):
            if offset + 3 > len(config):
                raise ValueError('Short HEVC array')
            offset += 1
            count = int.from_bytes(config[offset:offset + 2], 'big')
            offset += 2
            for _ in range(count):
                unit, offset = read_nal(config, offset, 2)
                sets.append(unit)
    else:
        if len(config) < 7:
            raise ValueError('Short avcC')
        length_size, offset = (config[4] & 3) + 1, 6
        for _ in range(config[5] & 31):
            unit, offset = read_nal(config, offset, 2)
            sets.append(unit)
        if offset >= len(config):
            raise ValueError('Missing PPS count')
        count, offset = config[offset], offset + 1
        for _ in range(count):
            unit, offset = read_nal(config, offset, 2)
            sets.append(unit)
    kinds = {(u[0] >> 1) & 63 if hevc else u[0] & 31 for u in sets}
    if not ({33, 34} if hevc else {7, 8}).issubset(kinds):
        raise ValueError('Missing video parameter sets')
    return length_size, b''.join(b'\0\0\0\1' + unit for unit in sets)


def read_nal(data, offset, length_size):
    if offset + length_size > len(data):
        raise ValueError('Truncated NAL length')
    size = int.from_bytes(data[offset:offset + length_size], 'big')
    offset += length_size
    if not 0 < size <= len(data) - offset:
        raise ValueError('Invalid NAL length')
    return data[offset:offset + size], offset + size


def parse_format(data):
    fields = dict(chunks(data))
    if fields.get('mdia') != tag('vide') or len(fields.get('vdim', b'')) != 8:
        raise ValueError('Missing video dimensions')
    width, height = struct.unpack('<II', fields['vdim'])
    if not 1 <= width <= 8192 or not 1 <= height <= 8192:
        raise ValueError('Video dimensions outside decoder limits')
    codec = fields.get('codc')
    hevc = codec in (tag('hvc1'), tag('hev1'))
    if not hevc and codec not in (tag('avc1'), tag('avc3')):
        raise ValueError('Unsupported video codec')
    extensions = fields.get('extn', b'')
    # Decoder config is a datv value nested in Apple's extension dictionary.
    for index in range(4, len(extensions) - 4):
        if extensions[index:index + 4] != tag('datv'):
            continue
        size = int.from_bytes(extensions[index - 4:index], 'little')
        if size < 8 or index - 4 + size > len(extensions):
            continue
        try:
            nalu_size, csd = parameter_sets(extensions[index + 4:index - 4 + size], hevc)
            return width, height, 'video/hevc' if hevc else 'video/avc', nalu_size, csd
        except ValueError:
            continue
    raise ValueError('Decoder parameter sets not found')


class Session:
    def __init__(self, write, video, diagnostic=lambda message: None):
        self.write, self.video = write, video
        self.diagnostic, self.control_packets = diagnostic, 0
        self.framer = Framer()
        self.epoch = time.monotonic_ns()
        self.received_ns = self.epoch
        self.video_clock = 0
        self.format = None
        self.frames = 0
        self.lock = threading.RLock()
        self.stopping = False
        self.handshake_started = False
        self.releases = 0
        self.released = threading.Event()

    def feed(self, data, received_ns=None):
        with self.lock:
            self.received_ns = received_ns if received_ns is not None else time.monotonic_ns()
            for payload in self.framer.feed(data):
                self.process(payload)

    def feed_java(self, data, received_ns):
        self.feed(bytes(data), received_ns)

    def format_java(self, data):
        width, height, mime, size, csd = parse_format(bytes(data))
        return width, height, mime, size, csd

    def process(self, packet):
        kind = packet[:4]
        if self.control_packets < 20:
            self.control_packets += 1
            subtype = packet[12:16] if kind in (tag('sync'), tag('asyn')) else b''
            self.diagnostic(f'QuickTime {kind[::-1]!r} {subtype[::-1]!r}: {len(packet)} bytes')
        if kind == tag('ping'):
            if not self.stopping:
                self.handshake_started = True
                self.write(chunk('ping', struct.pack('<II', 0, 1)))
            return
        if kind not in (tag('sync'), tag('asyn')):
            return
        if len(packet) < 16:
            raise ValueError('Short QuickTime control packet')
        clock = struct.unpack_from('<Q', packet, 4)[0]
        subtype = packet[12:16]
        if self.stopping:
            if kind == tag('sync') and subtype == tag('stop'):
                if len(packet) < 24:
                    raise ValueError('Missing correlation ID')
                self.write(chunk('rply', packet[16:24] + b'\0' * 8))
            elif kind == tag('asyn') and subtype == tag('rels'):
                self.releases += 1
                if self.releases >= 2:
                    self.released.set()
            # Do not request more media or restart negotiation during release.
            return
        if kind == tag('sync'):
            self.handshake_started = True
            if len(packet) < 24:
                raise ValueError('Missing correlation ID')
            correlation = packet[16:24]
            reply = b'\0' * 4
            if subtype in (tag('cwpa'), tag('cvrp')):
                if len(packet) < 32:
                    raise ValueError('Missing device clock')
                device_clock = int.from_bytes(packet[24:32], 'little')
                if subtype == tag('cwpa'):
                    self.write(control('hpd1', body=dictionary({
                        'Valeria': True, 'HEVCDecoderSupports444': False,
                        'DisplaySize': {'Width': 1080.0, 'Height': 2340.0}})))
                    reply += struct.pack('<Q', device_clock + 0x1000)
                else:
                    self.video_clock = device_clock
                    self.write(control('need', device_clock))
                    reply += struct.pack('<Q', device_clock + 0x1000af)
            elif subtype == tag('afmt'):
                reply += dictionary({'Error': 0})
            elif subtype == tag('clok'):
                reply += struct.pack('<Q', clock + 0x10000)
            elif subtype == tag('time'):
                reply += struct.pack('<qIIq', time.monotonic_ns() - self.epoch, 10**9, 1, 0)
            elif subtype == tag('skew'):
                reply += struct.pack('<d', 48000.0)
            elif subtype in (tag('go! '), tag('stop')):
                reply += b'\0' * 4
            else:
                return
            self.write(chunk('rply', correlation + reply))
        elif subtype == tag('feed'):
            if self.video_clock:
                # Request the next sample immediately after receiving this
                # complete packet, as the desktop capture loop does. Java
                # conversion and hardware decoding must not delay NEED.
                self.write(control('need', self.video_clock))
            self.media(packet[16:])

    def media(self, data):
        outer = list(chunks(data))
        if len(outer) != 1 or outer[0][0] != 'sbuf':
            raise ValueError('Invalid sample buffer envelope')
        fields = dict(chunks(outer[0][1]))
        if 'fdsc' in fields:
            self.format = parse_format(fields['fdsc'])
        if not self.format or not fields.get('sdat'):
            return
        width, height, mime, length_size, csd = self.format
        encoded, offset, units = fields['sdat'], 0, []
        while offset < len(encoded):
            unit, offset = read_nal(encoded, offset, length_size)
            units.append(b'\0\0\0\1' + unit)
        self.frames += 1
        self.video(b''.join(units), csd, width, height, mime,
                   self.received_ns // 1000)

    def stop(self):
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
            self.write(control('hpd0'))
            timeout = 6 if self.handshake_started else 1
        # The read/parser workers stay alive during shutdown. Never hold their
        # protocol lock while waiting for SYNC STOP and the two RELS packets.
        complete = self.released.wait(timeout)
        with self.lock:
            if complete:
                self.write(control('hpd0'))
            self.diagnostic(f'QuickTime release: notifications={self.releases}/2 complete={complete}')
