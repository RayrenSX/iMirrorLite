"""Parallel HID descriptors and iPhoneMirror's verified CoreDevice five-slot format.

CoreDevice mainTouchscreen has a fixed 58-byte layout even when the DDI
does not publish a report descriptor. Unknown surfaces retain single touch.
"""
from dataclasses import dataclass
import math
import struct
import time


@dataclass
class Field:
    report: int
    offset: int
    size: int
    page: int
    usage: int
    minimum: int
    maximum: int
    finger: int


def parse_descriptor(data):
    if not 1 <= len(data) <= 65536:
        raise ValueError('Invalid HID descriptor size')
    state = dict(page=0, minimum=0, maximum=0, size=0, count=0, report=0)
    stack, collections, usages = [], [], []
    offsets, fields = {}, []
    usage_min = None
    finger_number = 0
    i = 0
    while i < len(data):
        prefix = data[i]; i += 1
        if prefix == 0xfe:
            raise ValueError('HID long items are unsupported')
        size = (0, 1, 2, 4)[prefix & 3]
        if i + size > len(data):
            raise ValueError('Truncated HID item')
        raw = data[i:i + size]; i += size
        value = int.from_bytes(raw, 'little')
        kind, item = (prefix >> 2) & 3, prefix >> 4
        if kind == 1:
            if item == 0: state['page'] = value
            elif item == 1: state['minimum'] = int.from_bytes(raw, 'little', signed=True)
            elif item == 2: state['maximum'] = int.from_bytes(raw, 'little', signed=state['minimum'] < 0)
            elif item == 7: state['size'] = value
            elif item == 8:
                if not 1 <= value <= 255: raise ValueError('Invalid report ID')
                state['report'] = value
            elif item == 9: state['count'] = value
            elif item == 10: stack.append(state.copy())
            elif item == 11:
                if not stack: raise ValueError('HID global stack underflow')
                state = stack.pop()
        elif kind == 2:
            if item == 0: usages.append(value)
            elif item == 1: usage_min = value
            elif item == 2:
                if usage_min is None or not usage_min <= value <= usage_min + 512:
                    raise ValueError('Invalid HID usage range')
                usages.extend(range(usage_min, value + 1))
        elif kind == 0:
            if item == 10:
                finger = collections[-1] if collections else -1
                if state['page'] == 13 and usages and usages[0] == 0x22:
                    finger = finger_number; finger_number += 1
                collections.append(finger)
            elif item == 12:
                if not collections: raise ValueError('HID collection underflow')
                collections.pop()
            elif item in (8, 9, 11):
                bits = state['size'] * state['count']
                if (not 0 <= state['count'] <= 4096 or not 0 <= state['size'] <= 64
                        or (state['count'] and not state['size']) or not 0 <= bits <= 65536):
                    raise ValueError('HID field too large')
                key = (state['report'], item)
                offset = offsets.get(key, 8 if state['report'] else 0)
                if item == 8 and not value & 1 and value & 2:
                    for index in range(state['count']):
                        usage = usages[min(index, len(usages) - 1)] if usages else 0
                        fields.append(Field(state['report'], offset + index * state['size'],
                            state['size'], usage >> 16 or state['page'], usage & 65535,
                            state['minimum'], state['maximum'], collections[-1] if collections else -1))
                offsets[key] = offset + bits
            usages = []; usage_min = None
    if collections or stack: raise ValueError('Unclosed HID collection')
    return fields, {key[0]: (value + 7) // 8 for key, value in offsets.items() if key[1] == 8}


def descriptors(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if 'reportdescriptor' in key.lower():
                # CoreDevice wraps property values in Swift Codable envelopes.
                raw = child.get('data') if isinstance(child, dict) else child
                if isinstance(raw, bytes):
                    yield raw
            yield from descriptors(child)
    elif isinstance(value, (list, tuple)):
        for child in value: yield from descriptors(child)


def touchscreen_surface(value):
    if isinstance(value, dict):
        service = value.get('_ServiceID')
        if service == 257 or service == {'uint': 257}:
            return value
        for child in value.values():
            found = touchscreen_surface(child)
            if found is not None: return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = touchscreen_surface(child)
            if found is not None: return found
    return None


def build_touchscreen_frame(contacts, timestamp=None):
    """Ported from iPhoneMirror/tools/usb_touch_bridge.py; see FIVE_POINT_TOUCH.md.

    Contact slot is bits 0..4; touch/range are bits 6/7. Identity is indexed
    by slot at offset 40, and the 48-bit timestamp starts at offset 45.
    Lifted contacts remain in the snapshot until the release is delivered.
    """
    if not 1 <= len(contacts) <= 5:
        raise ValueError('touch report must contain one to five contacts')
    slots = [contact[0] for contact in contacts]
    if len(set(slots)) != len(slots) or any(type(slot) is not int or slot not in range(5) for slot in slots):
        raise ValueError('touch report slots must be unique and in range 0..4')
    if timestamp is None:
        timestamp = time.monotonic_ns() & ((1 << 48) - 1)
    report = bytearray(58)
    report[:3] = bytes([9, len(contacts), 5])
    for index, (slot, down, x, y) in enumerate(contacts):
        struct.pack_into('<BHH', report, 3 + index * 5, (0xc0 if down else 0) | slot, x, y)
        report[40 + slot] = slot + 1
    report[45:51] = timestamp.to_bytes(6, 'little')
    return bytes(report)


def is_coredevice_touchscreen(surface):
    return (isinstance(surface, dict)
            and surface.get('_ServiceID') in (257, {'uint': 257})
            and str(surface.get('Product', '')).startswith('CoreDevice touchscreen'))


class Encoder:
    def __init__(self, surface):
        self.capacity, self.fields, self.length, self.report = 1, [], 58, 9
        self.format = 'single'
        self.fingers = []
        for descriptor in descriptors(surface):
            try:
                fields, lengths = parse_descriptor(descriptor)
            except ValueError:
                continue
            for report, length in lengths.items():
                subset = [f for f in fields if f.report == report]
                fingers = []
                for finger in sorted({f.finger for f in subset if f.finger >= 0}):
                    required = {(1, 0x30), (1, 0x31), (13, 0x42), (13, 0x51)}
                    props = [f for f in subset if f.finger == finger and (f.page, f.usage) in required]
                    usages = {(f.page, f.usage) for f in props}
                    if (required == usages and len(props) == 4
                            and all(0 <= f.minimum <= f.maximum < 1 << f.size for f in props)
                            and all(f.minimum == 0 and f.maximum >= 1 for f in props if f.usage == 0x42)
                            and all(f.maximum > f.minimum for f in props if f.page == 1)):
                        fingers.append(finger)
                counts = [f for f in subset if (f.page, f.usage) == (13, 0x54) and f.finger == -1]
                if (len(counts) != 1 or counts[0].minimum != 0
                        or not 2 <= counts[0].maximum < 1 << counts[0].size):
                    continue
                capacity = min(5, len(fingers), counts[0].maximum)
                # Use a common range across fields. Adding each field's own
                # minimum to its slot can produce duplicate wire IDs.
                while capacity >= 2:
                    ids = [f for f in subset if f.finger in fingers[:capacity]
                           and (f.page, f.usage) == (13, 0x51)]
                    base = max(f.minimum for f in ids)
                    if base + capacity - 1 <= min(f.maximum for f in ids):
                        break
                    capacity -= 1
                if capacity >= 2 and length <= 4096:
                    self.capacity = capacity
                    self.contact_id_base = base
                    self.fields, self.length, self.report = subset, length, report
                    self.fingers = fingers[:self.capacity]
                    self.format = 'descriptor'
                    return
        if is_coredevice_touchscreen(surface):
            self.capacity, self.format = 5, 'coredevice'

    def encode(self, contacts):
        if self.format == 'coredevice':
            return build_touchscreen_frame([
                (p['slot'], p['down'], round(p['x'] * 65535), round(p['y'] * 65535))
                for p in sorted(contacts, key=lambda p: p['slot'])
            ] if contacts else [(0, False, 0, 0)])
        if self.capacity == 1:
            p = contacts[0] if contacts else dict(x=0, y=0, down=False, slot=0)
            state = 0xc2 if p['down'] else 2
            return (bytes([9, 1, 5, state]) + struct.pack('<HH', round(p['x'] * 65535), round(p['y'] * 65535))
                    + b'\0' * 32 + b'\x02\0\0\0'
                    + (time.monotonic_ns() & ((1 << 48) - 1)).to_bytes(6, 'little') + b'\0' * 8)
        data = bytearray(self.length)
        if self.report: data[0] = self.report
        count = sum(p['down'] for p in contacts)
        by_slot = {p['slot']: p for p in contacts}
        for f in self.fields:
            value = 0
            slot = self.fingers.index(f.finger) if f.finger in self.fingers else -1
            p = by_slot.get(slot)
            if f.page == 13 and f.usage == 0x54: value = count
            elif f.page == 13 and f.usage == 0x56:
                value = (time.monotonic_ns() // 100000) % (max(1, f.maximum) + 1)
            elif p is not None:
                if f.page == 1 and f.usage in (0x30, 0x31):
                    value = round(f.minimum + p['x' if f.usage == 0x30 else 'y'] * (f.maximum - f.minimum))
                elif f.page == 13 and f.usage == 0x51: value = self.contact_id_base + slot
                elif f.page == 13 and f.usage in (0x42, 0x32, 0x47): value = int(p['down'])
            if not 0 <= value < 1 << f.size: raise ValueError('HID value outside field range')
            for bit in range(f.size):
                if value >> bit & 1:
                    absolute = f.offset + bit
                    data[absolute // 8] |= 1 << (absolute % 8)
        return bytes(data)


class Contacts:
    def __init__(self, capacity):
        self.capacity, self.active = capacity, {}

    @staticmethod
    def validate(points):
        # Validate the entire batch before changing state.
        seen = set()
        for p in points:
            if (type(p.get('id')) is not int or p['id'] < 0 or p['id'] in seen
                or p.get('action') not in ('down', 'move', 'up')
                or any(not isinstance(p.get(k), (int, float)) or not math.isfinite(p[k])
                       or not 0 <= p[k] <= 1 for k in ('x', 'y'))):
                raise ValueError('Invalid touch batch')
            seen.add(p['id'])

    def apply(self, points):
        self.validate(points)
        release = []
        changed = False
        for p in points:
            identifier = p['id']
            if identifier not in self.active:
                if p['action'] != 'down' or len(self.active) >= self.capacity: continue
                used = {c['slot'] for c in self.active.values()}
                self.active[identifier] = {'slot': next(s for s in range(self.capacity) if s not in used)}
            self.active[identifier].update(x=p['x'], y=p['y'], down=p['action'] != 'up')
            changed = True
            if p['action'] == 'up': release.append(identifier)
        frame = [p.copy() for p in sorted(self.active.values(), key=lambda p: p['slot'])] if changed else []
        for identifier in release: del self.active[identifier]
        return frame

    def cancel(self):
        frame = [dict(p, down=False) for p in self.active.values()]
        self.active.clear()
        return frame


async def dispatch_touch(state, encoder, send, points=(), cancel=False):
    """Commit slot releases only after the report is sent, like the desktop bridge."""
    state.validate(points)
    batches = [points]
    if (any(p['action'] == 'up' and p['id'] in state.active for p in points)
            and any(p['action'] == 'down' and p['id'] not in state.active for p in points)):
        batches = [[p for p in points if p['action'] == 'up'],
                   [p for p in points if p['action'] != 'up']]
    for batch in batches:
        pending = Contacts(state.capacity)
        pending.active = {identifier: p.copy() for identifier, p in state.active.items()}
        frame = pending.cancel() if cancel else pending.apply(batch)
        if frame:
            try: await send(encoder.encode(frame))
            except BaseException:
                # An ambiguous DOWN may already have reached the phone. Keep
                # its slot in the cleanup snapshot until an UP is delivered.
                for point in batch:
                    if point['id'] not in state.active and point['id'] in pending.active:
                        state.active[point['id']] = pending.active[point['id']].copy()
                raise
        state.active = pending.active
