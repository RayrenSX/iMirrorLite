"""Cancellation and socket relay checks without USB hardware or input injection."""
import asyncio
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src/main/python'))
from imirror.runtime import Runtime
from imirror.mux_server import Server


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): pass


class Reader:
    async def read(self, count): await asyncio.Event().wait()
    async def readexactly(self, count): await asyncio.Event().wait()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_close_joins_idle_client_handlers(self):
        with tempfile.TemporaryDirectory() as root:
            server = Server(None, 'test-serial', 1, root)
            writer = Writer()
            task = asyncio.create_task(server.handle(Reader(), writer))
            await asyncio.sleep(0)
            self.assertEqual(len(server.handlers), 1)
            await asyncio.wait_for(server.close(), 1)
            self.assertTrue(task.done())
            self.assertTrue(writer.closed)
            self.assertFalse(server.handlers)
            self.assertFalse(server.clients)
            late = Writer()
            await server.handle(Reader(), late)
            self.assertTrue(late.closed)

    async def test_relay_flushes_fin_payload_before_closing(self):
        payload = [b'final TLS response', b'']
        conn = SimpleNamespace(closed=True, recv=lambda *args: payload.pop(0), close=lambda: None)
        writer = Writer()
        with tempfile.TemporaryDirectory() as root:
            server = Server(None, 'test-serial', 1, root)
            await asyncio.wait_for(server.relay(Reader(), writer, conn), 1)
        self.assertEqual(writer.data, b'final TLS response')


class TransportShutdownTests(unittest.TestCase):
    def test_usb_reader_remains_available_during_control_cleanup(self):
        incoming = queue.Queue()
        def read(channel):
            try: return incoming.get(timeout=0.05)
            except queue.Empty: return b''
        consumed = threading.Event()
        runtime = Runtime(SimpleNamespace(read=read),
                          SimpleNamespace(onDiagnostic=lambda text: None), '.')
        runtime.stop.set()  # HID/service cleanup is now in progress.
        runtime.start_reader(254, lambda data: consumed.set())
        try:
            incoming.put(b'final XPC acknowledgement')
            self.assertTrue(consumed.wait(1), 'USB stopped before control cleanup completed')
        finally:
            runtime.io_stop.set()
            for worker in runtime.workers: worker.join(1)
        self.assertFalse(any(worker.is_alive() for worker in runtime.workers))


if __name__ == '__main__': unittest.main()
