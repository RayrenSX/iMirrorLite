"""Direct LAN AirPlay + authenticated RemotePairing TCP/HID sessions."""
import asyncio
import ipaddress
import json
import plistlib
import ssl
import time
from contextlib import suppress
from pathlib import Path
from imirror.runtime import Runtime
from imirror.usb_tunnel import CoreDeviceTunnelProxy


def normalized_host(host):
    value = ipaddress.ip_address(host.split('%', 1)[0])
    if getattr(value, 'ipv4_mapped', None): value = value.ipv4_mapped
    return str(value)


class StreamService:
    def __init__(self, reader, writer): self.reader, self.writer = reader, writer
    async def sendall(self, data): self.writer.write(data); await self.writer.drain()
    async def recvall(self, size): return await self.reader.readexactly(size)
    async def close(self):
        self.writer.close()
        with suppress(Exception): await asyncio.wait_for(self.writer.wait_closed(), 3)


async def tcp_provider(pairing):
    if not hasattr(ssl.SSLContext, 'set_psk_client_callback'):
        raise RuntimeError('此 Python 运行时不支持无线控制所需的 TLS-PSK')
    params = await asyncio.wait_for(pairing.create_tcp_listener(), 10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # TLS authenticates the peer with the PSK obtained by signed Pair-Verify.
    pairing._set_psk_ciphers(context)
    context.set_psk_client_callback(lambda hint: (None, pairing.encryption_key))
    reader, writer = await asyncio.wait_for(asyncio.open_connection(pairing.hostname,
        int(params['port']), ssl=context, server_hostname=''), 12)
    return CoreDeviceTunnelProxy(StreamService(reader, writer))


async def bootstrap(rsd, runtime):
    """Create a wireless pair record over the trusted USB RSD connection."""
    from imirror.remote_pairing import CoreDeviceTunnelService
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    if rsd.udid and (Path(runtime.root)/'pairing'/('remote_'+rsd.udid+'.plist')).exists():
        return
    service = CoreDeviceTunnelService(rsd)
    try:
        runtime.status('diagnostic' if runtime.capacity else 'control', '正在准备首次无线配对，请确认 iPhone 上的信任提示')
        try:
            await asyncio.wait_for(service.connect(), 65)
        except RemotePairingCompletedError: pass
        if not service.pair_record_path.exists(): raise RuntimeError('无线配对记录未保存')
        runtime.callback.onDiagnostic('Wireless pairing record ready')
        runtime.status('diagnostic' if runtime.capacity else 'control', '无线配对已保存，下次可使用无线连接')
    finally:
        await service.close()


class WirelessRuntime(Runtime):
    def __init__(self, callback, root, name, width=1920, height=1080, fps=60):
        super().__init__(None, callback, root)
        if not (320<=width<=8192 and 180<=height<=8192 and fps in (24,30,60,120)):
            raise ValueError('Invalid wireless video profile')
        self.video_profile=(width,height,fps)
        self.name = name
        self.peer = None
        self.peer_address = None
        self.peer_generation = 0
        self.endpoints = {}
        self.endpoint_generation = 0

    def set_peer(self, host):
        self.peer_address = host
        host = normalized_host(host)
        if self.peer != host:
            self.peer = host; self.peer_generation += 1
            self.clear_aliases()
        self.first_video_at = None
        self.status('video', '无线投屏已连接，正在等待画面')

    def clear_peer(self, host):
        if self.peer == normalized_host(host):
            self.peer = None; self.peer_generation += 1
            self.peer_address = None
            self.clear_aliases()
            self.first_video_at = None
            self.submit({'cancel': True})

    def clear_aliases(self):
        changed = False
        for key,endpoint in list(self.endpoints.items()):
            if endpoint.get('source')=='mdns':
                self.endpoints.pop(key); changed = True
            elif 'hosts' in endpoint:
                self.endpoints[key] = {k:v for k,v in endpoint.items() if k!='hosts'}
                changed = True
        if changed: self.endpoint_generation += 1

    def device(self, raw):
        data = json.loads(str(raw))
        host = normalized_host(data['host'])
        port = int(data['port'])
        if not 1 <= port <= 65535: return
        kind = data.get('type', '').rstrip('.') + '.'
        if kind not in ('_remotepairing._tcp.', '_remotepairing-manual-pairing._tcp.'): return
        def update():
            key = (kind, data['name'].lower())
            previous = self.endpoints.get(key)
            if data.get('lost'): self.endpoints.pop(key, None)
            elif len(self.endpoints)<64 or key in self.endpoints:
                self.endpoints[key] = dict(data, host=host, address=data['host'], port=port, type=kind)
                if previous and previous['port']==port and host in previous.get('hosts',[]):
                    self.endpoints[key]['hosts']=previous['hosts']
            if previous != self.endpoints.get(key): self.endpoint_generation += 1
            if not data.get('lost'):
                self.callback.onDiagnostic(f'Wireless NSD: type={kind} host={host} port={port}')
        if self.loop:
            with suppress(RuntimeError): self.loop.call_soon_threadsafe(update)

    def candidates(self):
        return [e for e in self.endpoints.values() if self.peer in e.get('hosts',[e['host']])]

    async def resolve_peer(self):
        from imirror.discovery import resolve_peer
        generation = self.peer_generation
        if not self.peer_address: return
        try: endpoints = await resolve_peer(self.peer_address)
        except Exception as error:
            self.callback.onDiagnostic('Wireless address discovery: '+str(error))
            return
        if generation != self.peer_generation or self.stop.is_set(): return
        for endpoint in endpoints:
            key = (endpoint['type'],endpoint['name'])
            previous = self.endpoints.get(key)
            if len(self.endpoints)<64 or key in self.endpoints:
                self.endpoints[key] = endpoint
                if previous != endpoint: self.endpoint_generation += 1
                self.callback.onDiagnostic(f"Wireless DNS-SD: matched {len(endpoint['hosts'])} addresses to screen sender; dial={endpoint['address']}")

    async def connect_control(self, endpoint):
        from imirror.remote_pairing import RemotePairingTunnelService, RemotePairingManualPairingService
        from pymobiledevice3.exceptions import RemotePairingCompletedError
        records = sorted((Path(self.root)/'pairing').glob('remote_*.plist'))
        manual = endpoint['type'] == '_remotepairing-manual-pairing._tcp.'
        identifiers = [p.stem.removeprefix('remote_') for p in records]
        if manual: identifiers.append(endpoint.get('identifier') or endpoint['name'].split('.')[0])
        if not identifiers:
            raise RuntimeError('首次无线控制请先用 USB 连接一次，保存无线配对')
        last_error = None
        for identifier in identifiers[:16]:
            cls = RemotePairingManualPairingService if manual else RemotePairingTunnelService
            pairing = cls(identifier, endpoint.get('address',endpoint['host']), endpoint['port'])
            try:
                try: await asyncio.wait_for(pairing.connect(), 65 if manual else 10)
                except RemotePairingCompletedError:
                    raise RuntimeError('无线配对已保存，等待设备重新广播')
                # Ensure the control channel belongs to the screen sender's IP.
                provider = await tcp_provider(pairing)
                async with provider.start_tcp_tunnel() as tunnel:
                    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
                    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
                    from imirror.control import hid_control
                    async with UserspaceDialPlane(tunnel.client.tun, tunnel.address) as dial:
                        async with RemoteServiceDiscoveryService((tunnel.address, tunnel.port), open_connection=dial.dial) as rsd:
                            while not self.inputs.empty(): self.inputs.get_nowait()
                            await hid_control(self, rsd, tunnel)
                return
            except asyncio.CancelledError: raise
            except Exception as error:
                last_error = error
                if self.capacity: raise
            finally:
                await pairing.close()
        raise last_error or RuntimeError('无线配对不可用')

    async def run(self):
        if self.callback.isCancelled(): return
        from imirror.platform import configure
        configure(self.root)
        from imirror.airplay import Receiver
        self.loop = asyncio.get_running_loop()
        self.inputs = asyncio.Queue(maxsize=32)
        receiver = Receiver(self, self.name)
        controller = None
        generation = -1
        next_attempt = 0
        failures = 0
        endpoint_revision = -1
        active_endpoint = None
        discovery = None
        try:
            await receiver.start()
            self.status('video', '在 iPhone 控制中心 → 屏幕镜像中选择「'+self.name+'」')
            self.status('control', '等待 iPhone 投屏后自动连接无线控制')
            while not self.stop.is_set() and not self.callback.isCancelled():
                if generation != self.peer_generation:
                    if discovery:
                        discovery.cancel()
                        await asyncio.gather(discovery,return_exceptions=True)
                        discovery = None
                    if controller:
                        controller.cancel()
                        await asyncio.gather(controller, return_exceptions=True)
                    controller = None; active_endpoint = None; generation = self.peer_generation
                    self.control_ready(0); self.shortcuts_ready([])
                    while not self.inputs.empty(): self.inputs.get_nowait()
                    next_attempt = 0; failures = 0
                if endpoint_revision != self.endpoint_generation:
                    endpoint_revision = self.endpoint_generation
                    next_attempt = 0
                    if controller and active_endpoint not in self.endpoints.values():
                        controller.cancel()
                        await asyncio.gather(controller, return_exceptions=True)
                        controller = None; active_endpoint = None
                        self.control_ready(0); self.shortcuts_ready([])
                if controller and controller.done():
                    try: controller.result()
                    except asyncio.CancelledError: pass
                    except Exception as error:
                        self.status('control', str(error) or '无线控制已断开，正在重试')
                        self.callback.onDiagnostic('Wireless control: '+type(error).__name__+': '+str(error))
                    controller = None; active_endpoint = None; self.control_ready(0); self.shortcuts_ready([])
                    while not self.inputs.empty(): self.inputs.get_nowait()
                    failures += 1; next_attempt = time.monotonic() + min(30, 2**min(failures,5))
                if self.peer and not controller and time.monotonic() >= next_attempt:
                    if discovery and discovery.done():
                        await discovery
                        discovery = None
                    candidates = self.candidates()
                    endpoint = next((e for e in candidates if e['type']=='_remotepairing._tcp.'),
                                    next(iter(candidates),None))
                    if endpoint:
                        self.status('control', '正在连接无线控制，请保持 iPhone 解锁')
                        controller = asyncio.create_task(self.connect_control(endpoint))
                        active_endpoint = endpoint
                    else:
                        if not discovery: discovery = asyncio.create_task(self.resolve_peer())
                        paired = any((Path(self.root)/'pairing').glob('remote_*.plist'))
                        self.status('control', '正在查找已配对 iPhone 的无线控制服务' if paired else
                                    '尚未发现无线控制服务；首次请先通过 USB 完成配对')
                        next_attempt = time.monotonic()+5
                self.report_video_metrics()
                if self.first_video_at and time.monotonic()-self.first_video_at > 10 and not self.callback.hasRenderedVideo():
                    raise RuntimeError('已收到无线视频，但解码器未显示首帧，请查看诊断日志')
                await asyncio.sleep(.1)
        finally:
            self.stop.set()
            if discovery:
                discovery.cancel()
                await asyncio.gather(discovery,return_exceptions=True)
            if controller:
                controller.cancel()
                await asyncio.gather(controller, return_exceptions=True)
            self.control_ready(0); self.shortcuts_ready([])
            await receiver.close()
