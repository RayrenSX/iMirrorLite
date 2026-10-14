"""Pair -> mount DDI -> USB CoreDevice tunnel -> actual HID touch surface."""
import asyncio
import re
import uuid
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from imirror.hid import Contacts, Encoder, touchscreen_surface, dispatch_touch
from imirror import ddi
from imirror.shortcuts import open_shortcuts


def media_unsupported(error):
    # Only an explicit newer-DDI/older-iOS rejection permits direct HID fallback.
    detail = str(error)
    return ('startmediastream' in detail
            and re.search(r"\bcode['\"]?\s*:\s*9021\b", detail) is not None)


@asynccontextmanager
async def media_gate(rsd, tunnel, status):
    from pymobiledevice3.remote.core_device.display_service import DisplayService
    from imirror.usb_tunnel import UdpReceiver
    services = (rsd.peer_info or {}).get('Services', {})
    if DisplayService.SERVICE_NAME not in services:
        status('control', '设备未提供媒体认证，正在检查直接触控通道')
        yield 'direct'
        return
    unsupported = False
    async with AsyncExitStack() as stack:
        display = await stack.enter_async_context(DisplayService(rsd))
        udp = UdpReceiver(tunnel.client.tun.addr)
        stack.callback(udp.close)
        async def drain():
            while True:
                await udp.recv()
        drain_task = asyncio.create_task(drain())
        async def stop_drain():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        stack.push_async_callback(stop_drain)
        session = uuid.uuid4()
        async def stop_gate():
            with suppress(Exception):
                await asyncio.wait_for(display.stop_media_stream(session), 3)
        # Our request ID permits cleanup even if the response is lost.
        stack.push_async_callback(stop_gate)
        try:
            await asyncio.wait_for(display.start_video_stream(
                receiver_ip=udp.local_ip, receiver_port=udp.port,
                sender_ip=tunnel.address, display_id=1, client_session_id=session), 15)
        except Exception as error:
            if not media_unsupported(error):
                raise
            unsupported = True
        if not unsupported:
            await asyncio.sleep(0.3)
            yield 'mediastream'
            return
    status('control', '媒体认证返回 9021，正在检查直接触控通道')
    yield 'direct'


@asynccontextmanager
async def open_hid(rsd):
    from pymobiledevice3.remote.core_device.hid_service import UniversalHIDServiceService
    from pymobiledevice3.remote.xpc_message import XpcUInt64Type

    class LegacyHID(UniversalHIDServiceService):
        SERVICE_NAME = 'com.apple.coredevice.hid.universalhid'
        async def list_connected_services(self):
            return await self.service.send_receive_request({'messageType': 'Request',
                'featureIdentifier': 'com.apple.coredevice.feature.remote.universalhid',
                'payload': {'connectedServices': {}}})
        async def send_report(self, service_id, report):
            await self.service.send_request({'messageType': 'Request',
                'featureIdentifier': 'com.apple.coredevice.feature.remote.universalhid',
                'payload': {'send': {'_0': report, '_1': XpcUInt64Type(service_id)}}})
        async def create_keyboard_service(self, **kwargs):
            # The older endpoint accepts the same payload under its old feature name.
            from types import SimpleNamespace
            service = self.service
            class LegacyRequests:
                async def send_receive_request(self, request):
                    request = dict(request, featureIdentifier='com.apple.coredevice.feature.remote.universalhid')
                    return await service.send_receive_request(request)
            return await UniversalHIDServiceService.create_keyboard_service(
                SimpleNamespace(service=LegacyRequests()), **kwargs)

    services = (rsd.peer_info or {}).get('Services', {})
    errors = []
    for cls in (UniversalHIDServiceService, LegacyHID):
        if cls.SERVICE_NAME not in services:
            continue
        candidate = None
        try:
            candidate = cls(rsd)
            await asyncio.wait_for(candidate.__aenter__(), 8)
            inventory = await asyncio.wait_for(candidate.list_connected_services(), 8)
            surface = touchscreen_surface(inventory)
            if surface is None:
                raise RuntimeError('缺少 mainTouchscreen HID surface')
            candidate.inventory = inventory
        except BaseException as error:
            errors.append(type(error).__name__ + ': ' + str(error))
            if candidate is not None:
                with suppress(Exception):
                    await asyncio.wait_for(candidate.__aexit__(None, None, None), 3)
            if isinstance(error, asyncio.CancelledError): raise
            continue
        try:
            yield candidate, surface
        finally:
            await asyncio.wait_for(candidate.__aexit__(None, None, None), 3)
        return
    raise RuntimeError('iPhone 未发布可用的触控通道' + (': ' + '; '.join(errors) if errors else ''))


async def control(runtime, socket_path):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
    from imirror.usb_tunnel import CoreDeviceTunnelProxy

    runtime.status('control', '请解锁 iPhone，并在手机上确认「信任」')
    lockdown = await asyncio.wait_for(create_using_usbmux(serial=runtime.usb.serial,
        label='iMirrorLite', autopair=True, pair_timeout=60, usbmux_address=socket_path), 75)
    try:
        runtime.status('diagnostic', 'Connected device: ' + str(await lockdown.get_value(key='ProductType'))
                       + '; iOS ' + lockdown.product_version)
        if int(lockdown.product_version.split('.')[0]) < 18:
            raise RuntimeError('反向控制需要 iOS 18 或更高版本')
        runtime.status('control', '已配对，正在检查开发者环境')
        await ddi.ensure(lockdown, runtime.root, lambda s: runtime.status('control', s), runtime.stop.is_set)
        provider = await CoreDeviceTunnelProxy.create(lockdown)
        async with provider.start_tcp_tunnel() as tunnel:
            async with UserspaceDialPlane(tunnel.client.tun, tunnel.address) as dial:
                async with RemoteServiceDiscoveryService((tunnel.address, tunnel.port), open_connection=dial.dial) as rsd:
                    from imirror.wireless import bootstrap
                    async def prepare_wireless():
                        try: await bootstrap(rsd, runtime)
                        except Exception as error:
                            runtime.callback.onDiagnostic('Wireless pairing setup: ' + str(error))
                    preparation = asyncio.create_task(prepare_wireless())
                    try: await hid_control(runtime, rsd, tunnel)
                    finally:
                        preparation.cancel()
                        await asyncio.gather(preparation, return_exceptions=True)
    finally:
        await lockdown.close()


async def hid_control(runtime, rsd, tunnel):
    async with media_gate(rsd, tunnel, runtime.status) as mode:
        async with open_hid(rsd) as (hid, surface), open_shortcuts(rsd, hid, runtime, mode == 'mediastream') as shortcuts:
            encoder = Encoder(surface)
            state = Contacts(encoder.capacity)
            runtime.status('diagnostic', f'Touch report format={encoder.format}; capacity={encoder.capacity}')
            runtime.status('control', f'HID 通道已打开 · {mode} · {encoder.capacity} 指')
            runtime.control_ready(encoder.capacity)
            last_touch_sent = time.monotonic()
            async def send_touch(report):
                nonlocal last_touch_sent
                started = time.monotonic_ns()
                await asyncio.wait_for(hid.send_report(257, report), 3)
                last_touch_sent = time.monotonic()
                runtime.metric('touch_send', (time.monotonic_ns()-started)/1e9)
            try:
                while not runtime.stop.is_set():
                    if any(task.done() for task in tunnel.tasks):
                        raise ConnectionError('iPhone 控制隧道已断开')
                    try:
                        message = await asyncio.wait_for(runtime.inputs.get(), 0.25)
                    except asyncio.TimeoutError:
                        if state.active and time.monotonic()-last_touch_sent >= 1:
                            # Preserve stationary contacts with a fresh device-monotonic timestamp.
                            await send_touch(encoder.encode(list(state.active.values())))
                        continue
                    started = time.monotonic_ns()
                    if 'shortcut' in message:
                        await dispatch_touch(state, encoder, send_touch, cancel=True)
                        action = message['shortcut']
                        try:
                            await shortcuts.send(action)
                            runtime.status('diagnostic', 'Shortcut sent: ' + action)
                            runtime.callback.onShortcutResult(action, '')
                        except Exception as error:
                            runtime.status('diagnostic', 'Shortcut failed: ' + action + ': ' + str(error))
                            runtime.callback.onShortcutResult(action, '操作失败，请重试或重新连接')
                        continue
                    runtime.metric('touch_queue', (started-message.get('_received_ns', started))/1e9)
                    await dispatch_touch(state, encoder, send_touch,
                        points=message.get('points', []), cancel=bool(message.get('cancel')))
            finally:
                runtime.control_ready(0)
                released = state.cancel()
                if released:
                    with suppress(Exception):
                        await asyncio.wait_for(hid.send_report(257, encoder.encode(released)), 2)
