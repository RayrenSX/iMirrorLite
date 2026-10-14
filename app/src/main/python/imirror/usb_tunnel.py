"""CoreDeviceProxy TCP tunnel using Android's existing USB connection.

Wire protocol follows pymobiledevice3 11.3.1 (GPL-3.0), tunnel_service.py.
There is no kernel TUN interface and no network/QUIC fallback.
"""
import asyncio
import json
import struct
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

USE_USERSPACE_TUNNEL = True


class UdpReceiver:
    """Bind directly on this tunnel's address, without upstream CLI globals."""
    def __init__(self, address):
        from pmd_pytcp.socket import AF_INET6, SOCK_DGRAM, socket
        self.socket = socket(AF_INET6, SOCK_DGRAM)
        self.socket.bind((address, 0))
        self.local_ip, self.port = self.socket.getsockname()[:2]

    async def recv(self):
        return await self.socket.recv(65535)

    def close(self):
        self.socket.close()


class CoreDeviceTunnelProxy:
    SERVICE_NAME = 'com.apple.internal.devicecompute.CoreDeviceProxy'

    @classmethod
    async def create(cls, lockdown):
        return cls(await asyncio.wait_for(lockdown.start_lockdown_service(cls.SERVICE_NAME), 10))

    def __init__(self, service):
        self.service = service

    @asynccontextmanager
    async def start_tcp_tunnel(self):
        from pymobiledevice3.remote.userspace_tunnel import UserspaceTun
        tasks = []
        tun = None
        try:
            body = json.dumps({'type': 'clientHandshakeRequest', 'mtu': 16000}).encode()
            await asyncio.wait_for(self.service.sendall(b'CDTunnel' + struct.pack('>H', len(body)) + body), 10)
            header = await asyncio.wait_for(self.service.recvall(10), 10)
            if header[:8] != b'CDTunnel':
                raise ValueError('Invalid CoreDevice tunnel handshake')
            response = json.loads(await asyncio.wait_for(
                self.service.recvall(struct.unpack('>H', header[8:])[0]), 10))
            params = response['clientParameters']
            port = int(response['serverRSDPort'])
            if not 1<=port<=65535: raise ValueError('Invalid CoreDevice RSD port')
            tun = UserspaceTun('imirror-usb')
            tun.addr = params['address']
            tun.mtu = int(params['mtu'])
            if not 1280 <= tun.mtu <= 65535:
                raise ValueError('Invalid CoreDevice MTU')
            await tun.up()
            tun.set_peer(response['serverAddress'])

            async def receive():
                while True:
                    head = await self.service.recvall(40)
                    if len(head) != 40 or head[0] >> 4 != 6:
                        raise ConnectionError('Invalid IPv6 tunnel frame')
                    length = struct.unpack('>H', head[4:6])[0]
                    if length+40>tun.mtu: raise ConnectionError('IPv6 frame exceeds tunnel MTU')
                    tail = await self.service.recvall(length)
                    tun.write(head + tail)

            async def transmit():
                while True:
                    for packet in await tun.async_read_batch():
                        if packet and packet[0] >> 4 == 6:
                            await self.service.sendall(packet)

            tasks = [asyncio.create_task(receive()), asyncio.create_task(transmit())]
            yield SimpleNamespace(address=response['serverAddress'],
                                  port=port,
                                  client=SimpleNamespace(tun=tun), tasks=tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if tun is not None:
                with suppress(Exception):
                    await tun.close()
            await asyncio.wait_for(self.service.close(), 3)


@asynccontextmanager
async def start_tunnel(provider, **kwargs):
    async with provider.start_tcp_tunnel() as result:
        yield result
