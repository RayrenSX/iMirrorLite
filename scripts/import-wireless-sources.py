"""Import pinned protocol sources; no desktop CLI or privileged TUN backend."""
import ast
import hashlib
import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cache = ROOT / '.tools/wireless-wheels'
cache.mkdir(exist_ok=True)
metadata = json.load(urllib.request.urlopen('https://pypi.org/pypi/pymobiledevice3/11.3.1/json'))
entry = next(x for x in metadata['urls'] if x['filename'].endswith('.whl'))
wheel = cache / entry['filename']
if not wheel.exists():
    urllib.request.urlretrieve(entry['url'], wheel)
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == entry['digests']['sha256']
with zipfile.ZipFile(wheel) as archive:
    source = archive.read('pymobiledevice3/remote/tunnel_service.py').decode()
lines = source.splitlines(keepends=True)
tree = ast.parse(source)
keep = {'PairConsentResult', 'PairingDataComponentType', 'PairingDataComponentTLV8',
        'PairingDataComponentTLVBuf', 'RemotePairingProtocol', 'CoreDeviceTunnelService',
        'RemotePairingTunnelService', 'RemotePairingManualPairingService'}
pieces = []
for node in tree.body:
    name = getattr(node, 'name', None)
    if isinstance(node, ast.Assign):
        name = getattr(node.targets[0], 'id', None)
    if name not in keep:
        continue
    block = lines[node.lineno-1:node.end_lineno]
    if name == 'RemotePairingProtocol':
        removals = []
        for method in node.body:
            if getattr(method, 'name', None) in {'start_tcp_tunnel', 'start_quic_tunnel', 'create_quic_listener'}:
                first = min([method.lineno] + [d.lineno for d in method.decorator_list])
                removals.append((first-node.lineno, method.end_lineno-node.lineno+1))
        for start, end in sorted(removals, reverse=True):
            del block[start:end]
        block[0] = 'class RemotePairingProtocol(ABC):\n'
    if name == 'CoreDeviceTunnelService':
        block = [s for s in block if 'await self.rsd.close()' not in s]
    pieces.append(''.join(block))
imports = '''"""RemotePairing crypto/control protocol adapted from pymobiledevice3 11.3.1.

GPL-3.0. Source and wheel digest in third_party/provenance.json.
TCP tunnel transport lives in wireless.py; kernel TUN and QUIC are omitted.
"""
from __future__ import annotations
import asyncio, base64, hashlib, json, logging, os, platform, plistlib, ssl, struct
from abc import ABC, abstractmethod
from asyncio import StreamReader, StreamWriter
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple, Optional
from construct import Container, GreedyRange, Int8ul, Int64ul, Prefixed, GreedyBytes, Struct
from construct import Enum as ConstructEnum
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from srptools import SRPClientSession, SRPContext
from srptools.utils import hex_from
from srptools.constants import PRIME_3072, PRIME_3072_GEN
from opack2 import dumps
from pymobiledevice3.osu.os_utils import get_os_utils
from pymobiledevice3.exceptions import (PairingError, RemotePairingCompletedError,
    PyMobileDevice3Exception, UserDeniedPairingError, ConnectionTerminatedError, NotConnectedError,
    PskCipherNotSupportedError)
from pymobiledevice3.pair_records import (PAIRING_RECORD_EXT, create_pairing_records_cache_folder,
    generate_host_id, get_remote_pairing_record_filename)
from pymobiledevice3.remote.remote_service import RemoteService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService, parse_device_kvs_data
from pymobiledevice3.remote.xpc_message import XpcInt64Type, XpcUInt64Type
TIMEOUT = 8
OSUTIL = get_os_utils()
REPAIRING_PACKET_MAGIC = b'RPPairing'
class RPPairingPacketData:
    def __init__(self, body): self.body = body
class RPPairingPacket:
    @staticmethod
    def build(data):
        if len(data.body) > 65535: raise ValueError('RemotePairing packet too large')
        return REPAIRING_PACKET_MAGIC + struct.pack('>H', len(data.body)) + data.body
'''
result = imports + '\n\n'.join(pieces) + '\n'
result = result.replace('await reader.readexactly(len(REPAIRING_PACKET_MAGIC))',
    "magic = await reader.readexactly(len(REPAIRING_PACKET_MAGIC))\n        if magic != REPAIRING_PACKET_MAGIC:\n            raise ValueError('Invalid RemotePairing magic')")
result = result.replace('assert self.srp_context.verify_proof(data[PairingDataComponentType.PROOF].hex().encode())',
    "if not self.srp_context.verify_proof(data[PairingDataComponentType.PROOF].hex().encode()):\n            raise PairingError('Invalid SRP server proof')")
result = result.replace('        self.pair_record_path.write_bytes(\n',
    "        temporary = self.pair_record_path.with_suffix('.tmp')\n        temporary.write_bytes(\n")
result = result.replace('"remote_unlock_host_key": self.remote_unlock_host_key,',
    '"remote_unlock_host_key": self.remote_unlock_host_key,\n                "host_identifier": self.identifier,')
result = result.replace('        OSUTIL.chown_to_non_sudo_if_needed(self.pair_record_path)',
    '        temporary.chmod(0o600)\n        temporary.replace(self.pair_record_path)\n        OSUTIL.chown_to_non_sudo_if_needed(self.pair_record_path)')
(ROOT / 'app/src/main/python/imirror/remote_pairing.py').write_text(result, encoding='utf-8')
ux = ROOT / '.tools/UxPlay-aec205d49302df8d4eb291b9e927ed428b2d0166'
dest = ROOT / 'third_party/uxplay'
dest.mkdir(exist_ok=True)
shutil.copytree(ux / 'lib/playfair', dest / 'playfair', dirs_exist_ok=True)
shutil.copy(ux / 'LICENSE', dest / 'LICENSE')
# Keep the original table bytes, used by the protocol's first FairPlay response.
original = (ux / 'lib/fairplay_playfair.c').read_text()
values = re.findall(r'0x[0-9a-fA-F]+', original.split('char reply_message')[1].split('char fp_header')[0])
assert len(values) == 568
(ROOT / 'app/src/main/python/imirror/fairplay.bin').write_bytes(bytes(int(v,16) for v in values))
provenance_path = ROOT / 'third_party/provenance.json'
provenance = json.loads(provenance_path.read_text())
provenance['wireless'] = {'UxPlay': {'source': 'https://github.com/FDH2/UxPlay',
    'commit': 'aec205d49302df8d4eb291b9e927ed428b2d0166',
    'files': {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file()}},
    'pymobiledevice3': {'version': '11.3.1', 'wheel_sha256': entry['digests']['sha256'],
        'tunnel_service_sha256': hashlib.sha256(source.encode()).hexdigest()}}
provenance_path.write_text(json.dumps(provenance, indent=2) + '\n')
print('Imported pinned RemotePairing and PlayFair sources')
