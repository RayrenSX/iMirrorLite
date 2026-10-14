"""System operations matching iPhoneMirror's USB HID shortcuts."""
import asyncio
from contextlib import asynccontextmanager, suppress

BUTTONS = {'home': (0x40, .05), 'siri': (0xcf, 1),
           'volume_up': (0xe9, .05), 'volume_down': (0xea, .05),
           'lock': (0x30, .5)}
KEYS = {'control_center': 0x06, 'notification_center': 0x11}


def service_ids(value):
    if isinstance(value, dict):
        raw = value.get('_ServiceID')
        if isinstance(raw, dict): raw = raw.get('uint')
        if isinstance(raw, int) and not isinstance(raw, bool): yield raw
        for child in value.values(): yield from service_ids(child)
    elif isinstance(value, (list, tuple)):
        for child in value: yield from service_ids(child)


class Shortcuts:
    def __init__(self, indigo, hid, keyboard, stopped):
        self.indigo, self.hid, self.keyboard, self.stopped = indigo, hid, keyboard, stopped

    @property
    def actions(self):
        if self.indigo is None:
            return []
        return list(BUTTONS) + ['app_switcher'] + (list(KEYS) if self.keyboard is not None else [])

    async def button(self, code, hold):
        try:
            await asyncio.wait_for(self.indigo.send_button(0x0c, code, 1), 2)
            await asyncio.sleep(hold)
        finally:
            # Also release after a failed/ambiguous DOWN or task cancellation.
            await asyncio.wait_for(self.indigo.send_button(0x0c, code, 2), 2)

    async def send(self, action):
        if self.stopped() or action not in self.actions:
            raise RuntimeError('此操作当前不可用')
        if action == 'app_switcher':
            await self.button(*BUTTONS['home'])
            await asyncio.sleep(.1)
            if not self.stopped():
                await self.button(*BUTTONS['home'])
        elif action in BUTTONS:
            await self.button(*BUTTONS[action])
        else:
            try:
                await asyncio.wait_for(self.indigo.send_button(0x0c, 0x029d, 1), 2)
                if not self.stopped():
                    await asyncio.wait_for(self.hid.send_keyboard(self.keyboard, [KEYS[action]]), 2)
                    await asyncio.sleep(.02)
            finally:
                # Attempt both releases even when one fails.
                results = await asyncio.gather(
                    asyncio.wait_for(self.hid.send_keyboard(self.keyboard, []), 2),
                    asyncio.wait_for(self.indigo.send_button(0x0c, 0x029d, 2), 2),
                    return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result


@asynccontextmanager
async def open_shortcuts(rsd, hid, runtime, allow_create=False):
    from pymobiledevice3.remote.core_device.hid_service import IndigoHIDService
    indigo, keyboard, keyboard_hid = None, None, None
    try:
        candidate = IndigoHIDService(rsd)
        try:
            await asyncio.wait_for(candidate.__aenter__(), 5)
            indigo = candidate
        except BaseException as error:
            with suppress(Exception):
                await asyncio.wait_for(candidate.__aexit__(None, None, None), 3)
            if isinstance(error, asyncio.CancelledError): raise
            runtime.status('diagnostic', 'System buttons unavailable: ' + str(error))
        if indigo is not None:
            try:
                # Register on a separate connection: rejected createService can
                # close its socket, and must never break the live touchscreen.
                keyboard_hid = type(hid)(rsd)
                await asyncio.wait_for(keyboard_hid.__aenter__(), 5)
                ids = set(service_ids(hid.inventory))
                runtime.status('diagnostic', 'Shortcut HID surfaces: ' + str(sorted(ids)))
                if 512 in ids:
                    keyboard = 512
                elif allow_create:
                    keyboard = await asyncio.wait_for(keyboard_hid.create_keyboard_service(
                        product='iMirrorLite shortcuts', manufacturer='iMirrorLite'), 5)
                else:
                    runtime.status('diagnostic', 'Globe shortcuts unavailable: no authenticated keyboard surface')
            except Exception as error:
                runtime.status('diagnostic', 'Globe shortcuts unavailable: ' + str(error))
        shortcuts = Shortcuts(indigo, keyboard_hid, keyboard, runtime.stop.is_set)
        runtime.shortcuts_ready(shortcuts.actions)
        yield shortcuts
    finally:
        runtime.shortcuts_ready([])
        if keyboard is not None:
            with suppress(Exception):
                await asyncio.wait_for(keyboard_hid.send_keyboard(keyboard, []), 2)
        if keyboard_hid is not None:
            with suppress(Exception):
                await asyncio.wait_for(keyboard_hid.__aexit__(None, None, None), 2)
        if indigo is not None:
            with suppress(Exception):
                await asyncio.wait_for(indigo.__aexit__(None, None, None), 2)
