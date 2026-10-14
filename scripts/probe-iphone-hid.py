"""Inspect a trusted USB iPhone's HID capabilities without sending input.

This host-side development probe is not part of the APK transport. It uses
the same user-space tunnel as Android, and requires an already mounted DDI.
"""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/src/main/python'))
from imirror import usb_tunnel
sys.modules['pymobiledevice3.remote.tunnel_service'] = usb_tunnel
from imirror.hid import Encoder, touchscreen_surface
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.usbmux import list_devices
from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.core_device.hid_service import UniversalHIDServiceService


class LegacyHID(UniversalHIDServiceService):
    SERVICE_NAME = 'com.apple.coredevice.hid.universalhid'

    async def list_connected_services(self):
        return await self.service.send_receive_request({
            'messageType': 'Request',
            'featureIdentifier': 'com.apple.coredevice.feature.remote.universalhid',
            'payload': {'connectedServices': {}}})


async def main():
    devices = [d for d in await list_devices() if d.connection_type == 'USB']
    if len(devices) != 1:
        raise RuntimeError(f'Connect exactly one trusted USB iPhone; found {len(devices)}')
    result = {}
    async with await create_using_usbmux(serial=devices[0].serial, autopair=False) as lockdown:
        result.update(model=await lockdown.get_value(key='ProductType'), ios=lockdown.product_version)
        provider = await usb_tunnel.CoreDeviceTunnelProxy.create(lockdown)
        async with provider.start_tcp_tunnel() as tunnel:
            async with UserspaceDialPlane(tunnel.client.tun, tunnel.address) as dial:
                async with RemoteServiceDiscoveryService((tunnel.address, tunnel.port), open_connection=dial.dial) as rsd:
                    names = (rsd.peer_info or {}).get('Services', {})
                    result['services'] = [name for name in names if 'hid' in name or 'display' in name]
                    print('Services:', result['services'], flush=True)
                    for cls in (UniversalHIDServiceService, LegacyHID):
                        try:
                            async with cls(rsd) as hid:
                                inventory = await asyncio.wait_for(hid.list_connected_services(), 8)
                                surface = touchscreen_surface(inventory)
                                result[cls.SERVICE_NAME] = surface if surface is not None else inventory
                                print(cls.SERVICE_NAME, 'touchscreen:', surface is not None,
                                      'encoder capacity:', Encoder(surface).capacity, flush=True)
                        except Exception as error:
                            result[cls.SERVICE_NAME] = {'error': type(error).__name__, 'message': str(error)}
                            print(cls.SERVICE_NAME, type(error).__name__, str(error), flush=True)
    output = ROOT / 'artifacts/iphone-hid-probe.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2, default=lambda v: {'hex': v.hex()} if isinstance(v, bytes) else str(v)), encoding='utf-8')
    print('Saved:', output)


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), 45))
