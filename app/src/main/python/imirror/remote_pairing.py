"""RemotePairing crypto/control protocol adapted from pymobiledevice3 11.3.1.

GPL-3.0. Source and wheel digest in third_party/provenance.json.
TCP tunnel transport lives in wireless.py; kernel TUN and QUIC are omitted.
"""
from __future__ import annotations
import asyncio, base64, binascii, hashlib, json, logging, os, platform, plistlib, ssl, struct
import re
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
PairingDataComponentType = ConstructEnum(
    Int8ul,
    METHOD=0x00,
    IDENTIFIER=0x01,
    SALT=0x02,
    PUBLIC_KEY=0x03,
    PROOF=0x04,
    ENCRYPTED_DATA=0x05,
    STATE=0x06,
    ERROR=0x07,
    RETRY_DELAY=0x08,
    CERTIFICATE=0x09,
    SIGNATURE=0x0A,
    PERMISSIONS=0x0B,
    FRAGMENT_DATA=0x0C,
    FRAGMENT_LAST=0x0D,
    SESSION_ID=0x0E,
    TTL=0x0F,
    EXTRA_DATA=0x10,
    INFO=0x11,
    ACL=0x12,
    FLAGS=0x13,
    VALIDATION_DATA=0x14,
    MFI_AUTH_TOKEN=0x15,
    MFI_PRODUCT_TYPE=0x16,
    SERIAL_NUMBER=0x17,
    MFI_AUTH_TOKEN_UUID=0x18,
    APP_FLAGS=0x19,
    OWNERSHIP_PROOF=0x1A,
    SETUP_CODE_TYPE=0x1B,
    PRODUCTION_DATA=0x1C,
    APP_INFO=0x1D,
    SEPARATOR=0xFF,
)


PairingDataComponentTLV8 = Struct(
    "type" / PairingDataComponentType,
    "data" / Prefixed(Int8ul, GreedyBytes),
)


PairingDataComponentTLVBuf = GreedyRange(PairingDataComponentTLV8)


class PairConsentResult(NamedTuple):
    public_key: bytes
    salt: bytes
    pin: Optional[str]


class RemotePairingProtocol(ABC):
    WIRE_PROTOCOL_VERSION = 19

    def __init__(self):
        self.hostname: Optional[str] = None
        self._sequence_number = 0
        self._encrypted_sequence_number = 0
        self.version = None
        self.handshake_info = None
        self.x25519_private_key = X25519PrivateKey.generate()
        self.ed25519_private_key = Ed25519PrivateKey.generate()
        self.identifier = generate_host_id()
        self.srp_context = None
        self.encryption_key = None
        self.signature = None
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    async def receive_response(self) -> dict[str, Any]:
        pass

    @abstractmethod
    async def send_request(self, data: dict[str, Any]) -> None:
        pass

    async def send_receive_request(self, data: dict[str, Any]) -> dict[str, Any]:
        await self.send_request(data)
        return await self.receive_response()

    async def connect(self, autopair: bool = True) -> None:
        await self._attempt_pair_verify()

        if await self._validate_pairing():
            # Pairing record validation succeeded, so we can just initiate the relevant session keys
            self._init_client_server_main_encryption_keys()
            return

        if autopair:
            await self._pair()
            await self.close()

            # Once pairing is completed, the remote endpoint closes the connection, so it must be re-established
            raise RemotePairingCompletedError()


    async def create_tcp_listener(self) -> dict[str, Any]:
        assert self.encryption_key is not None
        request: dict[str, Any] = {
            "request": {
                "_0": {
                    "createListener": {
                        "key": base64.b64encode(self.encryption_key).decode(),
                        "peerConnectionsInfo": [{"owningPID": os.getpid(), "owningProcessName": "CoreDeviceService"}],
                        "transportProtocolType": "tcp",
                    }
                }
            }
        }
        response = await self._send_receive_encrypted_request(request)
        return response["createListener"]


    @staticmethod
    def _set_psk_ciphers(ctx: ssl.SSLContext) -> None:
        """Restrict the context to PSK ciphers, raising an actionable error if the SSL backend has none.

        Some SSL backends (notably LibreSSL, used by the macOS Command Line Tools python) ship without
        PSK cipher suites, so this fails with SSLError('No cipher can be selected.'). See issue #1570."""
        try:
            ctx.set_ciphers("PSK")
        except ssl.SSLError as e:
            raise PskCipherNotSupportedError(
                f"This python's SSL backend ({ssl.OPENSSL_VERSION}) cannot select the PSK ciphers required "
                f"for the TCP tunnel. Use an OpenSSL-based python build (e.g. Homebrew python3) instead."
            ) from e


    def save_pair_record(self) -> None:
        temporary = self.pair_record_path.with_suffix('.tmp')
        temporary.write_bytes(
            plistlib.dumps({
                "public_key": self.ed25519_private_key.public_key().public_bytes_raw(),
                "private_key": self.ed25519_private_key.private_bytes_raw(),
                "remote_unlock_host_key": self.remote_unlock_host_key,
                "host_identifier": self.identifier,
            })
        )
        temporary.chmod(0o600)
        temporary.replace(self.pair_record_path)
        OSUTIL.chown_to_non_sudo_if_needed(self.pair_record_path)

    @property
    def pair_record(self) -> Optional[dict[str, Any]]:
        if self.pair_record_path.exists():
            return plistlib.loads(self.pair_record_path.read_bytes())
        return None

    @property
    def remote_identifier(self) -> str:
        assert self.handshake_info is not None
        identifier = self.handshake_info["peerDeviceInfo"]["identifier"]
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', identifier):
            raise ValueError('Invalid RemotePairing device identifier')
        return identifier

    @property
    def remote_device_model(self) -> str:
        assert self.handshake_info is not None
        return self.handshake_info["peerDeviceInfo"]["model"]

    @property
    def auxiliary_metadata(self) -> dict[str, dict[str, Any]]:
        """Decoded ``deviceKVSData`` from the handshake, or ``{}`` before the handshake completed."""
        if self.handshake_info is None:
            return {}
        return parse_device_kvs_data(self.handshake_info.get("peerDeviceInfo", {}).get("deviceKVSData"))

    @property
    def pair_record_path(self) -> Path:
        pair_records_cache_directory = create_pairing_records_cache_folder()
        return (
            pair_records_cache_directory
            / f"{get_remote_pairing_record_filename(self.remote_identifier)}.{PAIRING_RECORD_EXT}"
        )

    async def _pair(self) -> None:
        pairing_consent_result = await self._request_pair_consent()
        self._init_srp_context(pairing_consent_result)
        await self._verify_proof()
        await self._save_pair_record_on_peer()
        self._init_client_server_main_encryption_keys()
        await self._create_remote_unlock()
        self.save_pair_record()

    async def _request_pair_consent(self) -> PairConsentResult:
        """Display a Trust / Don't Trust dialog"""

        tlv = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.METHOD, "data": b"\x00"},
            {"type": PairingDataComponentType.STATE, "data": b"\x01"},
        ])

        await self._send_pairing_data({
            "data": tlv,
            "kind": "setupManualPairing",
            "sendingHost": platform.node(),
            "startNewSession": True,
        })
        self.logger.info("Waiting user pairing consent")
        response = await self._receive_plain_response()
        response = response["event"]["_0"]

        pin = None
        if "pairingRejectedWithError" in response:
            raise PairingError(
                response["pairingRejectedWithError"]["wrappedError"]["userInfo"]["NSLocalizedDescription"]
            )
        elif "awaitingUserConsent" in response:
            pairing_data = await self._receive_pairing_data()
        else:
            # On tvOS no consent is needed and pairing data is returned immediately.
            pairing_data = self._decode_bytes_if_needed(response["pairingData"]["_0"]["data"])
            # On tvOS we need pin to setup pairing.
            if "AppleTV" in self.remote_device_model:
                pin = input("Enter PIN: ")

        data = self.decode_tlv(PairingDataComponentTLVBuf.parse(pairing_data))
        return PairConsentResult(
            public_key=data[PairingDataComponentType.PUBLIC_KEY], salt=data[PairingDataComponentType.SALT], pin=pin
        )

    def _init_srp_context(self, pairing_consent_result: PairConsentResult) -> None:
        # Receive server public and salt and process them.
        pin = pairing_consent_result.pin or "000000"
        client_session: Any = SRPClientSession(
            SRPContext("Pair-Setup", password=pin, prime=PRIME_3072, generator=PRIME_3072_GEN, hash_func=hashlib.sha512)
        )
        client_session.process(pairing_consent_result.public_key.hex(), pairing_consent_result.salt.hex())
        self.srp_context = client_session
        self.encryption_key = binascii.unhexlify(self.srp_context.key)

    async def _verify_proof(self) -> None:
        assert self.srp_context is not None
        client_public = binascii.unhexlify(self.srp_context.public)
        client_session_key_proof = binascii.unhexlify(self.srp_context.key_proof)

        tlv = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.STATE, "data": b"\x03"},
            {"type": PairingDataComponentType.PUBLIC_KEY, "data": client_public[:255]},
            {"type": PairingDataComponentType.PUBLIC_KEY, "data": client_public[255:]},
            {"type": PairingDataComponentType.PROOF, "data": client_session_key_proof},
        ])

        response = await self._send_receive_pairing_data({
            "data": tlv,
            "kind": "setupManualPairing",
            "sendingHost": platform.node(),
            "startNewSession": False,
        })
        data = self.decode_tlv(PairingDataComponentTLVBuf.parse(response))
        if not self.srp_context.verify_proof(data[PairingDataComponentType.PROOF].hex().encode()):
            raise PairingError('Invalid SRP server proof')

    async def _save_pair_record_on_peer(self) -> list[Any]:
        assert self.encryption_key is not None
        # HKDF with above computed key (SRP_compute_key) + Pair-Setup-Encrypt-Salt + Pair-Setup-Encrypt-Info
        # result used as key for chacha20-poly1305
        setup_encryption_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Pair-Setup-Encrypt-Salt",
            info=b"Pair-Setup-Encrypt-Info",
        ).derive(self.encryption_key)

        self.ed25519_private_key = Ed25519PrivateKey.generate()

        # HKDF with above computed key:
        #   (SRP_compute_key) + Pair-Setup-Controller-Sign-Salt + Pair-Setup-Controller-Sign-Info
        signbuf = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Pair-Setup-Controller-Sign-Salt",
            info=b"Pair-Setup-Controller-Sign-Info",
        ).derive(self.encryption_key)

        signbuf += self.identifier.encode()
        signbuf += self.ed25519_private_key.public_key().public_bytes_raw()

        self.signature = self.ed25519_private_key.sign(signbuf)

        device_info = dumps({
            "altIRK": b"\xe9\xe8-\xc0jIykVoT\x00\x19\xb1\xc7{",
            "btAddr": "11:22:33:44:55:66",
            "mac": b"\x11\x22\x33\x44\x55\x66",
            "remotepairing_serial_number": "AAAAAAAAAAAA",
            "accountID": self.identifier,
            "model": "computer-model",
            "name": platform.node(),
        })

        tlv = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.IDENTIFIER, "data": self.identifier.encode()},
            {
                "type": PairingDataComponentType.PUBLIC_KEY,
                "data": self.ed25519_private_key.public_key().public_bytes_raw(),
            },
            {"type": PairingDataComponentType.SIGNATURE, "data": self.signature},
            {"type": PairingDataComponentType.INFO, "data": device_info},
        ])

        cip = ChaCha20Poly1305(setup_encryption_key)
        encrypted_data = cip.encrypt(b"\x00\x00\x00\x00PS-Msg05", tlv, b"")

        tlv = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.ENCRYPTED_DATA, "data": encrypted_data[:255]},
            {"type": PairingDataComponentType.ENCRYPTED_DATA, "data": encrypted_data[255:]},
            {"type": PairingDataComponentType.STATE, "data": b"\x05"},
        ])

        response = await self._send_receive_pairing_data({
            "data": tlv,
            "kind": "setupManualPairing",
            "sendingHost": platform.node(),
            "startNewSession": False,
        })
        data = self.decode_tlv(PairingDataComponentTLVBuf.parse(response))

        tlv = PairingDataComponentTLVBuf.parse(
            cip.decrypt(b"\x00\x00\x00\x00PS-Msg06", data[PairingDataComponentType.ENCRYPTED_DATA], b"")
        )

        return tlv

    def _init_client_server_main_encryption_keys(self) -> None:
        assert self.encryption_key is not None
        client_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=None,
            info=b"ClientEncrypt-main",
        ).derive(self.encryption_key)
        self.client_cip = ChaCha20Poly1305(client_key)

        server_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=None,
            info=b"ServerEncrypt-main",
        ).derive(self.encryption_key)
        self.server_cip = ChaCha20Poly1305(server_key)

    async def _create_remote_unlock(self) -> None:
        try:
            response = await self._send_receive_encrypted_request({"request": {"_0": {"createRemoteUnlockKey": {}}}})
            self.remote_unlock_host_key = response["createRemoteUnlockKey"]["hostKey"]
        except PyMobileDevice3Exception:
            # tvOS does not support remote unlock.
            self.remote_unlock_host_key = ""

    async def _attempt_pair_verify(self) -> None:
        self.handshake_info = await self._send_receive_handshake({
            "hostOptions": {"attemptPairVerify": True},
            "wireProtocolVersion": XpcInt64Type(self.WIRE_PROTOCOL_VERSION),
        })

    @staticmethod
    def _decode_bytes_if_needed(data: bytes) -> bytes:
        return data

    async def _validate_pairing(self) -> bool:
        pairing_data = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.STATE, "data": b"\x01"},
            {
                "type": PairingDataComponentType.PUBLIC_KEY,
                "data": self.x25519_private_key.public_key().public_bytes_raw(),
            },
        ])
        response = await self._send_receive_pairing_data({
            "data": pairing_data,
            "kind": "verifyManualPairing",
            "startNewSession": True,
        })
        data = self.decode_tlv(PairingDataComponentTLVBuf.parse(response))

        if PairingDataComponentType.ERROR in data:
            await self._send_pair_verify_failed()
            return False

        peer_public_key = X25519PublicKey.from_public_bytes(data[PairingDataComponentType.PUBLIC_KEY])
        self.encryption_key = self.x25519_private_key.exchange(peer_public_key)

        derived_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Pair-Verify-Encrypt-Salt",
            info=b"Pair-Verify-Encrypt-Info",
        ).derive(self.encryption_key)
        cip = ChaCha20Poly1305(derived_key)

        # TODO:
        #   we should be able to verify from the received encrypted data, but from some reason we failed to
        #   do so. instead, we verify using the next stage

        if self.pair_record is None:
            private_key = Ed25519PrivateKey.from_private_bytes(b"\x00" * 0x20)
        else:
            private_key = Ed25519PrivateKey.from_private_bytes(self.pair_record["private_key"])
            # A device-initiated pairing record stores the identifier the host registered
            # itself under; present that same identifier during pair-verify so the device
            # recognizes us instead of re-prompting.
            host_identifier = self.pair_record.get("host_identifier")
            if host_identifier:
                self.identifier = host_identifier

        signbuf = b""
        signbuf += self.x25519_private_key.public_key().public_bytes_raw()
        signbuf += self.identifier.encode()
        signbuf += peer_public_key.public_bytes_raw()

        signature = private_key.sign(signbuf)

        encrypted_data = cip.encrypt(
            b"\x00\x00\x00\x00PV-Msg03",
            PairingDataComponentTLVBuf.build([
                {"type": PairingDataComponentType.IDENTIFIER, "data": self.identifier.encode()},
                {"type": PairingDataComponentType.SIGNATURE, "data": signature},
            ]),
            b"",
        )

        pairing_data = PairingDataComponentTLVBuf.build([
            {"type": PairingDataComponentType.STATE, "data": b"\x03"},
            {"type": PairingDataComponentType.ENCRYPTED_DATA, "data": encrypted_data},
        ])

        response = await self._send_receive_pairing_data({
            "data": pairing_data,
            "kind": "verifyManualPairing",
            "startNewSession": False,
        })
        data = self.decode_tlv(PairingDataComponentTLVBuf.parse(response))

        if PairingDataComponentType.ERROR in data:
            await self._send_pair_verify_failed()
            return False

        return True

    async def _send_pair_verify_failed(self) -> None:
        await self._send_plain_request({"event": {"_0": {"pairVerifyFailed": {}}}})

    async def _send_receive_encrypted_request(self, request: dict[str, Any]) -> dict[str, Any]:
        nonce = Int64ul.build(self._encrypted_sequence_number) + b"\x00" * 4
        encrypted_data = self.client_cip.encrypt(nonce, json.dumps(request).encode(), b"")

        response = await self.send_receive_request({
            "message": {"streamEncrypted": {"_0": encrypted_data}},
            "originatedBy": "host",
            "sequenceNumber": XpcUInt64Type(self._sequence_number),
        })
        self._encrypted_sequence_number += 1

        encrypted_data = self._decode_bytes_if_needed(response["message"]["streamEncrypted"]["_0"])
        plaintext = self.server_cip.decrypt(nonce, encrypted_data, None)
        response = json.loads(plaintext)["response"]["_1"]

        if "errorExtended" in response:
            raise PyMobileDevice3Exception(response["errorExtended"]["_0"]["userInfo"]["NSLocalizedDescription"])

        return response

    async def _send_receive_handshake(self, handshake_data: dict[str, Any]) -> dict[str, Any]:
        response = await self._send_receive_plain_request({"request": {"_0": {"handshake": {"_0": handshake_data}}}})
        return response["response"]["_1"]["handshake"]["_0"]

    async def _send_receive_pairing_data(self, pairing_data: dict[str, Any]) -> bytes:
        await self._send_pairing_data(pairing_data)
        return await self._receive_pairing_data()

    async def _send_pairing_data(self, pairing_data: dict[str, Any]) -> None:
        await self._send_plain_request({"event": {"_0": {"pairingData": {"_0": pairing_data}}}})

    async def _receive_pairing_data(self) -> bytes:
        response = await self._receive_plain_response()
        response = response["event"]["_0"]
        if "pairingData" in response:
            return self._decode_bytes_if_needed(response["pairingData"]["_0"]["data"])
        if "pairingRejectedWithError" in response:
            raise UserDeniedPairingError(
                response["pairingRejectedWithError"]
                .get("wrappedError", {})
                .get("userInfo", {})
                .get("NSLocalizedDescription")
            )
        raise PyMobileDevice3Exception(f"Got an unknown state message: {response}")

    async def _send_receive_plain_request(self, plain_request: dict[str, Any]):
        await self._send_plain_request(plain_request)
        return await self._receive_plain_response()

    async def _send_plain_request(self, plain_request: dict[str, Any]) -> None:
        await self.send_request({
            "message": {"plain": {"_0": plain_request}},
            "originatedBy": "host",
            "sequenceNumber": XpcUInt64Type(self._sequence_number),
        })
        self._sequence_number += 1

    async def _receive_plain_response(self) -> dict[str, Any]:
        response = await self.receive_response()
        return response["message"]["plain"]["_0"]

    @staticmethod
    def decode_tlv(tlv_list: list[Container[Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for tlv in tlv_list:
            if tlv.type in result:
                result[tlv.type] += tlv.data
            else:
                result[tlv.type] = tlv.data
        return result

    async def __aenter__(self) -> "RemotePairingProtocol":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


class CoreDeviceTunnelService(RemotePairingProtocol, RemoteService):
    SERVICE_NAME = "com.apple.internal.dt.coredevice.untrusted.tunnelservice"

    def __init__(self, rsd: RemoteServiceDiscoveryService):
        RemoteService.__init__(self, rsd, self.SERVICE_NAME)
        RemotePairingProtocol.__init__(self)
        self.version: Optional[int] = None

    async def connect(self, autopair: bool = True) -> None:
        # Establish RemoteXPC connection to `SERVICE_NAME`
        await RemoteService.connect(self)
        try:
            response = await self.service.receive_response()
            self.version = response["ServiceVersion"]

            # Perform pairing if necessary and start a trusted RemoteXPC connection
            await RemotePairingProtocol.connect(self, autopair=autopair)
            self.hostname = self.service.address[0]
        except Exception:
            await self.service.close()
            raise

    async def close(self) -> None:
        if self._service is not None:
            await self._service.close()

    async def receive_response(self) -> dict[str, Any]:
        response = await self.service.receive_response()
        return response["value"]

    async def send_request(self, data: dict[str, Any]) -> None:
        return await self.service.send_request({
            "mangledTypeName": "RemotePairing.ControlChannelMessageEnvelope",
            "value": data,
        })


class RemotePairingTunnelService(RemotePairingProtocol):
    def __init__(self, remote_identifier: str, hostname: str, port: int) -> None:
        RemotePairingProtocol.__init__(self)
        self._remote_identifier = remote_identifier
        self.hostname = hostname
        self.port = port
        self._reader: Optional[StreamReader] = None
        self._writer: Optional[StreamWriter] = None

    @property
    def remote_identifier(self) -> str:
        return self._remote_identifier

    @property
    def reader(self) -> StreamReader:
        """The stream reader.

        :raises NotConnectedError: if accessed before ``connect()`` (or after ``close()``).
        """
        if self._reader is None:
            raise NotConnectedError("RemotePairingTunnelService is not connected; call connect() first")
        return self._reader

    @property
    def writer(self) -> StreamWriter:
        """The stream writer.

        :raises NotConnectedError: if accessed before ``connect()`` (or after ``close()``).
        """
        if self._writer is None:
            raise NotConnectedError("RemotePairingTunnelService is not connected; call connect() first")
        return self._writer

    async def connect(self, autopair: bool = True) -> None:
        connect_task = asyncio.create_task(asyncio.open_connection(self.hostname, self.port))
        try:
            self._reader, self._writer = await asyncio.wait_for(connect_task, timeout=TIMEOUT)
        except BaseException:
            if not connect_task.done():
                connect_task.cancel()
                with suppress(asyncio.CancelledError):
                    await connect_task
            await self.close()
            raise

        try:
            await self._attempt_pair_verify()
            if not await self._validate_pairing():
                raise ConnectionTerminatedError()
            self._init_client_server_main_encryption_keys()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._writer is None:
            return
        writer = self._writer
        self._writer = None
        self._reader = None
        writer.close()
        with suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 3)

    async def receive_response(self) -> dict[str, Any]:
        reader = self.reader
        magic = await reader.readexactly(len(REPAIRING_PACKET_MAGIC))
        if magic != REPAIRING_PACKET_MAGIC:
            raise ValueError('Invalid RemotePairing magic')
        size = struct.unpack(">H", await reader.readexactly(2))[0]
        return json.loads(await reader.readexactly(size))

    async def send_request(self, data: dict[str, Any]) -> None:
        writer = self.writer
        writer.write(
            RPPairingPacket.build(
                RPPairingPacketData(body=json.dumps(data, default=self._default_json_encoder).encode())
            )
        )
        await writer.drain()

    @staticmethod
    def _default_json_encoder(obj: Any) -> str:
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        raise TypeError()

    @staticmethod
    def _decode_bytes_if_needed(data: bytes) -> bytes:
        return base64.b64decode(data)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} IDENTIFIER:{self.remote_identifier} HOSTNAME:{self.hostname} PORT:{self.port}>"
        )


class RemotePairingManualPairingService(RemotePairingTunnelService):
    async def connect(self, autopair: bool = True) -> None:
        fut = asyncio.open_connection(self.hostname, self.port)
        self._reader, self._writer = await asyncio.wait_for(fut, timeout=TIMEOUT)
        await RemotePairingProtocol.connect(self, autopair=autopair)

