"""Private AF_UNIX usbmuxd adapter. Pair records never leave app storage."""
import asyncio
import hashlib
import os
from pathlib import Path
import plistlib
import socket
import struct
import uuid
from contextlib import suppress
from imirror.mux import ConnectionRefused, MuxError


class Server:
    def __init__(self, mux, serial, product, root):
        self.mux, self.serial, self.product = mux, serial, product
        self.root = Path(root)
        self.records = self.root / 'pairing'
        self.records.mkdir(exist_ok=True)
        buid = self.records / 'buid'
        if not buid.exists(): buid.write_text(str(uuid.uuid4()).upper())
        self.buid = buid.read_text()
        self.path = str(self.root / 'mux.sock')
        self.clients = set()
        self.handlers = set()
        self.closed = False
        self.server = None

    async def start(self):
        with suppress(FileNotFoundError): os.unlink(self.path)
        self.server = await asyncio.start_unix_server(self.handle, path=self.path)
        os.chmod(self.path, 0o600)

    async def close(self):
        self.closed = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for client in list(self.clients): client.close()
        handlers = list(self.handlers)
        for task in handlers: task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        with suppress(FileNotFoundError): os.unlink(self.path)

    def device(self):
        return {'DeviceID': 1, 'MessageType': 'Attached', 'Properties': {
            'DeviceID': 1, 'ConnectionType': 'USB', 'SerialNumber': self.serial,
            'USBSerialNumber': self.serial.replace('-', ''), 'ProductID': self.product,
            'LocationID': 0, 'ConnectionSpeed': 480000000}}

    @staticmethod
    def frame(tag, data):
        body = plistlib.dumps(data)
        return struct.pack('<IIII', len(body) + 16, 1, 8, tag) + body

    async def handle(self, reader, writer):
        if self.closed:
            writer.close()
            return
        task = asyncio.current_task()
        self.handlers.add(task)
        self.clients.add(writer)
        conn = None
        try:
            while True:
                length, version, kind, tag = struct.unpack('<IIII', await reader.readexactly(16))
                if not 16 <= length <= 2 * 1024 * 1024:
                    raise ValueError('Invalid usbmux host packet')
                body = await reader.readexactly(length - 16)
                if kind != 8:
                    writer.write(struct.pack('<IIIII', 20, 1, 1, tag, 1))
                    await writer.drain()
                    return
                request = plistlib.loads(body)
                mt = request.get('MessageType')
                response = {'MessageType': 'Result', 'Number': 0}
                if mt == 'ReadBUID': response = {'BUID': self.buid}
                elif mt == 'ListDevices': response = {'DeviceList': [self.device()]}
                elif mt == 'Listen':
                    writer.write(self.frame(tag, response) + self.frame(tag, self.device()))
                    await writer.drain()
                    await reader.read()
                    return
                elif mt in ('ReadPairRecord', 'SavePairRecord', 'DeletePairRecord'):
                    identifier = request.get('PairRecordID', '')
                    if identifier.replace('-', '').lower() != self.serial.replace('-', '').lower():
                        response['Number'] = 2
                    else:
                        name = hashlib.sha256(self.serial.encode()).hexdigest() + '.plist'
                        path = self.records / name
                        if mt == 'ReadPairRecord':
                            response = {'PairRecordData': path.read_bytes()} if path.exists() else {'Number': 2}
                        elif mt == 'SavePairRecord':
                            record = request['PairRecordData']
                            if not isinstance(record, bytes): raise ValueError('Invalid pair record')
                            plistlib.loads(record)
                            temporary = path.with_suffix('.tmp')
                            temporary.write_bytes(record)
                            os.chmod(temporary, 0o600)
                            temporary.replace(path)
                        else:
                            with suppress(FileNotFoundError): path.unlink()
                elif mt == 'Connect':
                    if request.get('DeviceID') != 1: response['Number'] = 2
                    else:
                        port = socket.ntohs(int(request['PortNumber']))
                        try:
                            conn = await asyncio.to_thread(self.mux.connect, port, 10)
                        except (ConnectionRefused, MuxError): response['Number'] = 3
                        else:
                            writer.write(self.frame(tag, response)); await writer.drain()
                            await self.relay(reader, writer, conn)
                            return
                else: response['Number'] = 1
                writer.write(self.frame(tag, response)); await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            if conn is not None:
                with suppress(Exception): await asyncio.to_thread(conn.close)
            self.handlers.discard(task)
            self.clients.discard(writer)
            writer.close()
            with suppress(Exception): await asyncio.wait_for(writer.wait_closed(), 3)

    async def relay(self, reader, writer, conn):
        async def upload():
            while data := await reader.read(32768):
                await asyncio.to_thread(conn.send, data)

        async def download():
            while True:
                data = await asyncio.to_thread(conn.recv, 32768, 0.5)
                if data:
                    writer.write(data); await writer.drain()
                elif conn.closed:
                    break

        tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await asyncio.to_thread(conn.close)
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
