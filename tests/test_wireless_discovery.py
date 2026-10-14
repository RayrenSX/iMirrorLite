"""DNS-SD wire records and mixed IPv4/IPv6 control selection."""
import asyncio
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/src/main/python'))
from imirror.discovery import Records, resolve_peer
from imirror.wireless import WirelessRuntime
from pymobiledevice3.bonjour import _build_rr, _encode_srv, encode_name

TYPE = '_remotepairing._tcp.local.'
INSTANCE = 'Phone.'+TYPE
HOST = 'Phone.local.'


def response(port=49152, ttl=120, ipv4='192.168.137.199', ipv6='fe80::8d0:d92c:4470:85cf'):
    records = [_build_rr(TYPE,12,encode_name(INSTANCE),ttl,False),
        _build_rr(INSTANCE,33,_encode_srv(0,0,port,HOST),ttl,True),
        _build_rr(HOST,1,socket.inet_pton(socket.AF_INET,ipv4),ttl,True),
        _build_rr(HOST,28,socket.inet_pton(socket.AF_INET6,ipv6),ttl,True)]
    return struct.pack('!HHHHHH',0,0x8400,0,len(records),0,0)+b''.join(records)


def runtime():
    return WirelessRuntime(SimpleNamespace(onStatus=lambda *args:None,onDiagnostic=lambda text:None),'.','test')


class RecordsTests(unittest.TestCase):
    def test_ipv6_screen_sender_matches_ipv4_of_same_srv_host(self):
        records=Records(); records.feed(response())
        matches=records.for_peer('fe80::8d0:d92c:4470:85cf%wlan0')
        self.assertEqual(len(matches),1)
        self.assertEqual(matches[0]['address'],'192.168.137.199')
        self.assertIn('fe80::8d0:d92c:4470:85cf',matches[0]['hosts'])

    def test_neighbor_in_same_subnet_is_not_candidate(self):
        records=Records(); records.feed(response())
        self.assertFalse(records.for_peer('192.168.137.200'))
        self.assertFalse(records.for_peer('fe80::1234'))

    def test_goodbye_revokes_service_and_invalid_packet_is_ignored(self):
        records=Records(); records.feed(response())
        records.feed(b'bad'); records.feed(response(ttl=0))
        self.assertFalse(records.for_peer('192.168.137.199'))

    def test_ipv6_only_retains_interface_scope(self):
        records=Records(); records.feed(response())
        records.addresses[HOST.lower()].discard('192.168.137.199')
        self.assertEqual(records.for_peer('fe80::8d0:d92c:4470:85cf%40')[0]['address'],
                         'fe80::8d0:d92c:4470:85cf%40')


class RuntimeAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_alias_resolution_then_native_update_and_goodbye(self):
        current=runtime(); current.loop=asyncio.get_running_loop()
        current.set_peer('fe80::8d0:d92c:4470:85cf%40')
        records=Records(); records.feed(response()); endpoints=records.for_peer(current.peer_address)
        async def resolve(peer): return endpoints
        with patch('imirror.discovery.resolve_peer',resolve): await current.resolve_peer()
        self.assertEqual(current.candidates()[0]['address'],'192.168.137.199')
        current.device('{"name":"PHONE","type":"_remotepairing._tcp.","host":"192.168.137.199","port":49152}')
        await asyncio.sleep(0)
        self.assertEqual(len(current.candidates()),1)
        current.device('{"name":"PHONE","type":"_remotepairing._tcp.","host":"192.168.137.199","port":49152,"lost":true}')
        await asyncio.sleep(0)
        self.assertFalse(current.candidates())

    async def test_new_screen_sender_cannot_reuse_old_aliases(self):
        current=runtime(); current.set_peer('fe80::8d0:d92c:4470:85cf%40')
        records=Records(); records.feed(response())
        endpoint=records.for_peer(current.peer_address)[0]
        current.endpoints[(endpoint['type'],endpoint['name'])]=endpoint
        current.clear_peer(current.peer)
        current.set_peer('192.168.137.199')
        self.assertFalse(current.candidates())

    async def test_delayed_old_sender_resolution_is_discarded(self):
        current=runtime(); current.set_peer('192.168.137.199')
        records=Records(); records.feed(response())
        async def resolve(peer):
            current.set_peer('192.168.137.200')
            return records.for_peer(peer)
        with patch('imirror.discovery.resolve_peer',resolve): await current.resolve_peer()
        self.assertFalse(current.endpoints)

    async def test_real_udp_query_matches_both_address_families(self):
        loop=asyncio.get_running_loop()
        peers=[]
        closed=loop.create_future()
        class Responder(asyncio.DatagramProtocol):
            def connection_made(self,transport): self.transport=transport
            def datagram_received(self,data,peer):
                peers.append(peer)
                self.transport.sendto(response(),peer)
            def connection_lost(self,error):
                if not closed.done(): closed.set_result(None)
        transport,_=await loop.create_datagram_endpoint(Responder,local_addr=('127.0.0.1',0))
        target=transport.get_extra_info('sockname')
        try:
            result=await resolve_peer('fe80::8d0:d92c:4470:85cf%40',query_target=target)
            self.assertEqual(result[0]['host'],'192.168.137.199')
            self.assertNotEqual(peers[0][1],5353)
        finally: transport.close(); await asyncio.wait_for(closed,1)
