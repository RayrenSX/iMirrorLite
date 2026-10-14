"""Resolve a screen sender's control service with all DNS-SD host addresses.

Android 12 NSD returns one address, while AirPlay can use another address of
the same iPhone. A/AAAA records must belong to the same SRV target; a shared
subnet alone is never sufficient. Queries use ephemeral unicast-response
sockets, leaving port 5353 to Android's system mDNS daemon.
"""
import asyncio
import ipaddress
import socket
import struct
import time
from contextlib import suppress
from pymobiledevice3.bonjour import build_query, parse_mdns_message

SERVICE_TYPES = ('_remotepairing._tcp.', '_remotepairing-manual-pairing._tcp.')


def normalize(address):
    value = ipaddress.ip_address(address.split('%',1)[0])
    return str(getattr(value,'ipv4_mapped',None) or value)


class Records:
    def __init__(self):
        self.instances, self.services, self.addresses, self.txt = {}, {}, {}, {}

    def feed(self, packet):
        try: records = parse_mdns_message(packet)
        except (ValueError, IndexError, struct.error, UnicodeError): return
        for record in records[:128]:
            name = record['name'].lower()
            alive = record.get('ttl',0) > 0
            kind = record['type']
            if kind == 12:
                service = next((t for t in SERVICE_TYPES if name == t+'local.'),None)
                if service:
                    instance = record['ptrdname'].lower()
                    if alive and (len(self.instances)<64 or instance in self.instances):
                        self.instances[instance] = service
                    elif not alive:
                        self.instances.pop(instance,None); self.services.pop(instance,None)
            elif kind == 33:
                if alive and 1 <= record['port'] <= 65535 and (len(self.services)<64 or name in self.services):
                    self.services[name] = (record['target'].lower(),record['port'])
                elif not alive: self.services.pop(name,None)
            elif kind in (1,28) and 'address' in record:
                if name not in self.addresses and len(self.addresses)>=64: continue
                values = self.addresses.setdefault(name,set())
                address = normalize(record['address'])
                if alive and len(values)<16: values.add(address)
                elif not alive: values.discard(address)
            elif kind == 16 and alive and len(self.txt)<64:
                self.txt[name] = record.get('txt',{})

    def queries(self):
        result = [(instance,33) for instance in self.instances if instance not in self.services]
        for target,_ in self.services.values(): result.extend(((target,1),(target,28)))
        return result

    def for_peer(self, peer):
        normalized = normalize(peer)
        result = []
        for instance,service_type in self.instances.items():
            target,port = self.services.get(instance,(None,None))
            addresses = self.addresses.get(target,set())
            if normalized not in addresses: continue
            host = next((a for a in sorted(addresses) if ':' not in a),normalized)
            address = peer if host == normalized else host
            suffix = service_type+'local.'
            result.append(dict(name=instance.removesuffix('.'+suffix),type=service_type,
                host=host,address=address,port=port,hosts=sorted(addresses),
                identifier=self.txt.get(instance,{}).get('identifier'),source='mdns'))
        return result


async def resolve_peer(peer, timeout=1.5, query_target=None):
    records = Records()
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=128)
    class Protocol(asyncio.DatagramProtocol):
        def __init__(self): self.closed = loop.create_future()
        def datagram_received(self, data, sender):
            if len(data)<=9000 and not queue.full(): queue.put_nowait(data)
        def connection_lost(self, error):
            if not self.closed.done(): self.closed.set_result(None)
    transports, destinations, protocols = [], [], []
    try:
        sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP,socket.IP_MULTICAST_TTL,255)
        sock.bind(('0.0.0.0',0)); sock.setblocking(False)
        protocol=Protocol()
        try: transport,_ = await loop.create_datagram_endpoint(lambda:protocol,sock=sock)
        except BaseException: sock.close(); raise
        protocols.append(protocol)
        transports.append(transport); destinations.append(query_target or ('224.0.0.251',5353))
        if query_target is None and ':' in peer and '%' in peer:
            scope = peer.rsplit('%',1)[1]
            index = int(scope) if scope.isdigit() else socket.if_nametoindex(scope)
            sock6 = socket.socket(socket.AF_INET6,socket.SOCK_DGRAM)
            sock6.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_MULTICAST_HOPS,255)
            sock6.bind(('::',0)); sock6.setblocking(False)
            protocol6=Protocol()
            try: transport6,_ = await loop.create_datagram_endpoint(lambda:protocol6,sock=sock6)
            except BaseException: sock6.close(); raise
            protocols.append(protocol6)
            transports.append(transport6); destinations.append(('ff02::fb',5353,0,index))
        sent = set()
        def query(name,kind):
            if (name,kind) in sent or len(sent)>=192: return
            sent.add((name,kind))
            packet = build_query(name,kind,unicast=True)
            for transport,destination in zip(transports,destinations): transport.sendto(packet,destination)
        for kind in SERVICE_TYPES: query(kind+'local.',12)
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            try: data = await asyncio.wait_for(queue.get(),deadline-time.monotonic())
            except asyncio.TimeoutError: break
            records.feed(data)
            matches = records.for_peer(peer)
            if matches and any(':' not in m['host'] for m in matches): return matches
            for name,kind in records.queries(): query(name,kind)
        return records.for_peer(peer)
    finally:
        for transport in transports: transport.close()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*(p.closed for p in protocols)),1)
