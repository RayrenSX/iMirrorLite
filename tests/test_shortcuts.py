"""Shortcut releases must survive transport errors and cancellation."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src/main/python'))
from imirror.shortcuts import Shortcuts, service_ids
from imirror.runtime import Runtime
from types import SimpleNamespace


class HID:
    def __init__(self):
        self.events = []
        self.fail_down = False
        self.fail_release = False
        self.down = asyncio.Event()

    async def send_button(self, page, code, state):
        self.events.append(('button', page, code, state))
        if state == 1:
            self.down.set()
            if self.fail_down: raise ConnectionError('ambiguous DOWN')

    async def send_keyboard(self, service, keys):
        self.events.append(('keyboard', service, keys))
        if not keys and self.fail_release: raise ConnectionError('release failed')


class ShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_owned_keyboard_surface_in_swift_envelope(self):
        inventory = {'connectedServices': [{'_ServiceID': 257},
                     {'_CoreDevice_codablePropertyStorage': {'_ServiceID': {'uint': 512}}},
                     {'_ServiceID': True}]}
        self.assertEqual(set(service_ids(inventory)), {257, 512})
    async def test_failed_down_still_releases(self):
        hid = HID(); hid.fail_down = True
        with self.assertRaises(ConnectionError):
            await Shortcuts(hid, hid, 99, lambda: False).send('home')
        self.assertEqual(hid.events[-1], ('button', 12, 0x40, 2))

    async def test_cancel_during_hold_releases(self):
        hid = HID()
        task = asyncio.create_task(Shortcuts(hid, hid, 99, lambda: False).send('siri'))
        await hid.down.wait(); await asyncio.sleep(.01); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(hid.events[-1], ('button', 12, 0xcf, 2))

    async def test_globe_released_when_keyboard_release_fails(self):
        hid = HID(); hid.fail_release = True
        with self.assertRaises(ConnectionError):
            await Shortcuts(hid, hid, 99, lambda: False).send('control_center')
        self.assertIn(('keyboard', 99, [6]), hid.events)
        self.assertIn(('keyboard', 99, []), hid.events)
        self.assertIn(('button', 12, 0x29d, 2), hid.events)

    async def test_app_switcher_two_home_pulses(self):
        hid = HID()
        await Shortcuts(hid, hid, None, lambda: False).send('app_switcher')
        self.assertEqual(hid.events, [('button', 12, 0x40, state) for state in (1, 2, 1, 2)])

    async def test_stop_blocks_new_operations_and_missing_keyboard_limits_capabilities(self):
        hid = HID(); shortcuts = Shortcuts(hid, hid, None, lambda: True)
        self.assertNotIn('control_center', shortcuts.actions)
        with self.assertRaises(RuntimeError): await shortcuts.send('home')
        self.assertEqual(hid.events, [])

    async def test_touch_cancel_preserves_shortcut_completion(self):
        runtime = Runtime(None, SimpleNamespace(onDiagnostic=lambda text: None), '.')
        runtime.loop = asyncio.get_running_loop(); runtime.inputs = asyncio.Queue(maxsize=32)
        runtime.inputs.put_nowait({'points': [1]})
        runtime.inputs.put_nowait({'shortcut': 'home'})
        runtime.submit({'cancel': True}); await asyncio.sleep(0)
        self.assertTrue(runtime.inputs.get_nowait()['cancel'])
        self.assertEqual(runtime.inputs.get_nowait()['shortcut'], 'home')

    async def test_stop_after_submit_prevents_late_enqueue(self):
        runtime = Runtime(None, SimpleNamespace(onDiagnostic=lambda text: None), '.')
        runtime.loop = asyncio.get_running_loop(); runtime.inputs = asyncio.Queue(maxsize=32)
        runtime.submit({'shortcut': 'home'}); runtime.stop.set(); await asyncio.sleep(0)
        self.assertTrue(runtime.inputs.empty())
