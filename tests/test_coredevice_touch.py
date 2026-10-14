"""Independently decode the layout checked by iPhoneMirror against dtuhidd."""
import asyncio
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src/main/python'))
from imirror.hid import Encoder, Contacts, build_touchscreen_frame, dispatch_touch

SURFACE = {'_ServiceID': 257, 'Product': 'CoreDevice touchscreen(nil)'}


def decode(report):
    assert len(report) == 58 and report[0] == 9 and report[2] == 5
    result = {}
    for index in range(report[1]):
        flags, x, y = struct.unpack_from('<BHH', report, 3 + index * 5)
        slot = flags & 31
        assert slot not in result
        result[slot] = (bool(flags & 64), bool(flags & 128), report[40 + slot], x, y)
    return result


def point(identifier, action, x=.3, y=.5):
    return dict(id=identifier, action=action, x=x, y=y)


class CoreDeviceLayoutTests(unittest.TestCase):
    def test_known_device_surface_without_descriptor_has_five_slots(self):
        encoder = Encoder(SURFACE)
        self.assertEqual((encoder.capacity, encoder.format), (5, 'coredevice'))
        self.assertEqual(Encoder({}).capacity, 1)
        self.assertEqual(Encoder({'_ServiceID': 257, 'Product': 'Unknown'}).capacity, 1)

    def test_five_identities_flags_timestamp_and_reserved_offsets(self):
        report = build_touchscreen_frame([(s, True, 1000+s, 2000+s) for s in range(5)], 0x010203040506)
        self.assertEqual(decode(report), {s: (True, True, s+1, 1000+s, 2000+s) for s in range(5)})
        self.assertEqual(report[45:51], bytes.fromhex('060504030201'))
        self.assertEqual(report[28:40], bytes(12))
        self.assertEqual(report[51:], bytes(7))

    def test_released_identity_is_indexed_by_slot_not_record(self):
        self.assertEqual(decode(build_touchscreen_frame([(4, False, 123, 456)], 1)),
                         {4: (False, False, 5, 123, 456)})

    def test_duplicate_invalid_slots_or_six_contacts_rejected(self):
        for slots in ([], [0, 0], [-1], [5], list(range(6))):
            with self.assertRaises(ValueError):
                build_touchscreen_frame([(s, True, 0, 0) for s in slots])


class CoreDeviceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.encoder, self.state, self.frames = Encoder(SURFACE), Contacts(5), []

    async def send(self, report): self.frames.append(report)

    async def apply(self, points=(), cancel=False):
        await dispatch_touch(self.state, self.encoder, self.send, points, cancel)

    async def test_full_snapshots_independent_release_sixth_rejection_and_reuse(self):
        await self.apply([point(i+10, 'down', .1*(i+1)) for i in range(5)])
        original = decode(self.frames[-1])
        await self.apply([point(12, 'move', .7, .8)])
        moved = decode(self.frames[-1])
        for slot in (0, 1, 3, 4): self.assertEqual(original[slot], moved[slot])
        self.assertEqual(moved[2][3:], (round(.7*65535), round(.8*65535)))
        count = len(self.frames)
        await self.apply([point(99, 'down')])
        self.assertEqual(len(self.frames), count)
        await self.apply([point(12, 'up', .7, .8)])
        released = decode(self.frames[-1])
        self.assertEqual(released[2][:3], (False, False, 3))
        self.assertTrue(all(v[0] for s, v in released.items() if s != 2))
        await self.apply([point(99, 'move')])
        self.assertNotIn(99, self.state.active)
        await self.apply([point(99, 'down')])
        self.assertEqual(self.state.active[99]['slot'], 2)
        await self.apply(cancel=True)
        self.assertFalse(self.state.active)
        self.assertEqual(len(decode(self.frames[-1])), 5)
        self.assertTrue(all(not v[0] and not v[1] for v in decode(self.frames[-1]).values()))

    async def test_release_commit_waits_for_transport_success(self):
        await self.apply([point(10, 'down')])
        async def fail(report): raise ConnectionError('send failed')
        with self.assertRaises(ConnectionError):
            await dispatch_touch(self.state, self.encoder, fail, [point(10, 'up')])
        self.assertTrue(self.state.active[10]['down'])
        await self.apply(cancel=True)
        self.assertFalse(decode(self.frames[-1])[0][0])

    async def test_full_batch_replacement_delivers_release_before_slot_reuse(self):
        await self.apply([point(i, 'down') for i in range(5)])
        await self.apply([point(2, 'up'), point(99, 'down', .9)])
        self.assertFalse(decode(self.frames[-2])[2][0])
        self.assertTrue(decode(self.frames[-1])[2][0])
        self.assertEqual(self.state.active[99]['slot'], 2)
        self.assertEqual(len(decode(self.frames[-1])), 5)

    async def test_invalid_second_half_cannot_partially_release(self):
        await self.apply([point(1, 'down')])
        count = len(self.frames)
        with self.assertRaises(ValueError):
            await self.apply([point(1, 'up'), point(99, 'down', float('nan'))])
        self.assertEqual(len(self.frames), count)
        self.assertTrue(self.state.active[1]['down'])

    async def test_cancelled_write_keeps_contacts_for_final_cleanup(self):
        await self.apply([point(1, 'down')])
        async def interrupted(report): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await dispatch_touch(self.state, self.encoder, interrupted, cancel=True)
        self.assertIn(1, self.state.active)
        await self.apply(cancel=True)
        self.assertFalse(self.state.active)
