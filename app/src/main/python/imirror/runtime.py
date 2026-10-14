"""One bounded USB or wireless session per Android process; via Chaquopy."""
import asyncio
import json
import logging
import os
import queue
import threading
import time
import traceback
from contextlib import suppress
from pathlib import Path
from imirror import quicktime
from imirror.mux import MuxDevice
from imirror.mux_server import Server

_current = None
_lock = threading.Lock()


def error_detail(error):
    # Chaquopy Java exceptions may stringify to just the class name.
    message = getattr(error, 'getMessage', None)
    return str(message() if callable(message) else error) or type(error).__name__


class Runtime:
    def __init__(self, usb, callback, root):
        self.usb, self.callback, self.root = usb, callback, root
        self.stop = threading.Event()
        self.io_stop = threading.Event()
        self.loop = None
        self.inputs = None
        self.capacity = 0
        self.workers = []
        self.last_video = time.monotonic()
        self.video_frames = 0
        self.first_video_at = None
        self.failure = None
        self.video_metrics = {name: [0, 0, 0] for name in ('read', 'convert', 'decode', 'queue', 'video_queue', 'write', 'touch_queue', 'touch_send')}
        self.metrics_lock = threading.Lock()
        self.metrics_at = time.monotonic()
        self.byte_array = None
        self.video_callback = None
        self.video_timed_callback = None
        self.video_config = None
        self.java_video_config = None
        self.video = quicktime.Session(lambda data: self.write(42, data), self.on_video,
                                      self.callback.onDiagnostic)

    def write(self, channel, data):
        if self.byte_array is None:
            from java import jarray, jbyte
            self.byte_array = jarray(jbyte)
            self.video_callback = self.callback.onVideo
        started = time.monotonic()
        self.usb.write(channel, self.byte_array(data))
        if channel == 42:
            self.metric('write', time.monotonic() - started)

    def status(self, kind, message):
        self.callback.onStatus(kind, message)

    def control_ready(self, capacity):
        self.capacity = capacity
        self.callback.onControl(capacity)

    def shortcuts_ready(self, actions):
        self.callback.onShortcuts(','.join(actions))

    def on_video(self, data, csd, width, height, mime, pts):
        self._on_video(data,csd,width,height,mime,pts)

    def on_video_timed(self, data, csd, width, height, mime, pts, source_us):
        self._on_video(data,csd,width,height,mime,pts,source_us)

    def _on_video(self, data, csd, width, height, mime, pts, source_us=None):
        if self.stop.is_set() or self.callback.isCancelled(): return
        if self.byte_array is None:
            from java import jarray, jbyte
            self.byte_array = jarray(jbyte)
            self.video_callback = self.callback.onVideo
        self.last_video = time.monotonic()
        if self.first_video_at is None:
            self.first_video_at = self.last_video
        self.video_frames += 1
        started = time.monotonic()
        frame = self.byte_array(data)
        if csd != self.video_config:
            self.callback.onDiagnostic('Video decoder config: ' + csd.hex())
            self.java_video_config = self.byte_array(csd)
            self.video_config = csd
        converted = time.monotonic()
        if source_us is None:
            self.video_callback(frame, self.java_video_config, width, height, mime, pts)
        else:
            if self.video_timed_callback is None: self.video_timed_callback = self.callback.onVideoTimed
            self.video_timed_callback(frame, self.java_video_config, width, height, mime, pts, source_us)
        finished = time.monotonic()
        self.metric('convert', converted - started)
        self.metric('decode', finished - converted)

    def metric(self, name, elapsed):
        with self.metrics_lock:
            values = self.video_metrics[name]
            values[0] += 1
            values[1] += elapsed
            values[2] = max(values[2], elapsed)

    def report_video_metrics(self):
        now = time.monotonic()
        if now - self.metrics_at < 5:
            return
        with self.metrics_lock:
            values = self.video_metrics
            self.video_metrics = {name: [0, 0, 0] for name in values}
        details = '; '.join(f'{name}: n={n} avg_ms={1000*total/max(1,n):.2f} max_ms={1000*peak:.2f}'
                            for name, (n, total, peak) in values.items() if n)
        if details:
            self.callback.onDiagnostic(f'Runtime performance ({now-self.metrics_at:.1f}s): {details}')
        self.metrics_at = now

    def fail(self, error):
        if not self.stop.is_set():
            self.failure = error_detail(error)
            self.stop.set()

    def start_reader(self, channel, consume, failed=None):
        # Parsing/ACK writes use a separate worker so USB reads keep draining.
        pending = queue.Queue(maxsize=128)
        abort = threading.Event()
        def fail_reader(error):
            abort.set()
            (failed or self.fail)(error)
        def read():
            first = True
            try:
                while not self.io_stop.is_set() and not abort.is_set():
                    started = time.monotonic()
                    data = bytes(self.usb.read(channel))
                    if channel == 42:
                        self.metric('read', time.monotonic() - started)
                    if data:
                        if first:
                            first = False
                            self.callback.onDiagnostic(f'USB channel {channel}: first read {len(data)} bytes')
                        pending.put((data, time.monotonic_ns()), timeout=2)
            except Exception as error:
                fail_reader(error)
        def parse():
            try:
                while not self.io_stop.is_set() and not abort.is_set():
                    try: data, received_ns = pending.get(timeout=0.25)
                    except queue.Empty: continue
                    if channel == 42:
                        self.metric('queue', (time.monotonic_ns() - received_ns) / 1e9)
                        consume(data, received_ns)
                    else:
                        consume(data)
            except Exception as error:
                fail_reader(error)
        for target in (read, parse):
            worker = threading.Thread(target=target, daemon=True)
            self.workers.append(worker)
            worker.start()

    def submit(self, message):
        if not self.loop or not self.inputs or self.stop.is_set(): return
        message['_received_ns'] = time.monotonic_ns()
        def enqueue():
            if self.stop.is_set(): return
            if message.get('cancel') or self.inputs.full():
                # No lost UP/DOWN. On overload, cancel all contacts and abort
                # the gesture. Preserve queued button operations so their UI
                # completion cannot be lost to a touch cancel/rotation.
                shortcuts = []
                while not self.inputs.empty():
                    queued = self.inputs.get_nowait()
                    if 'shortcut' in queued: shortcuts.append(queued)
                self.inputs.put_nowait({'cancel': True})
                for queued in shortcuts[:self.inputs.maxsize - 1]:
                    self.inputs.put_nowait(queued)
                if 'shortcut' in message and not self.inputs.full(): self.inputs.put_nowait(message)
                return
            self.inputs.put_nowait(message)
        with suppress(RuntimeError): self.loop.call_soon_threadsafe(enqueue)

    async def run(self):
        if self.callback.isCancelled():
            return
        self.loop = asyncio.get_running_loop()
        self.inputs = asyncio.Queue(maxsize=32)
        mux = MuxDevice(lambda data: self.write(254, data), self.usb.muxPacketSize(), self.usb.serial)
        server = Server(mux, self.usb.serial, self.usb.productId, self.root)
        controller = None
        video_stream = None
        try:
            video_stream = self.usb.startVideo(self.video, self.callback)
            control_failure = []
            def fail_mux(error):
                if not control_failure: control_failure.append(error_detail(error))
                mux.close_all()
            self.start_reader(254, mux.feed, fail_mux)
            await server.start()
            os.environ['USBMUXD_SOCKET_ADDRESS'] = server.path
            from imirror.platform import configure
            configure(self.root)
            from imirror.control import control
            async def start_control():
                await asyncio.to_thread(mux.start)
                if not await asyncio.to_thread(mux.ready.wait, 8):
                    raise ConnectionError('iPhone 未响应 USB 控制握手；视频连接继续运行')
                if control_failure: raise ConnectionError(control_failure[0])
                if not self.stop.is_set(): await control(self, server.path)
            controller = asyncio.create_task(start_control())
            control_error_reported = False
            video_woken = False
            while not self.stop.is_set():
                frames = video_stream.frameCount()
                if frames != self.video_frames:
                    self.video_frames = frames
                    self.last_video = time.monotonic()
                    if self.first_video_at is None:
                        self.first_video_at = self.last_video
                if video_stream.failure():
                    raise ConnectionError(str(video_stream.failure()))
                self.report_video_metrics()
                if self.callback.isCancelled():
                    self.stop.set()
                    break
                if not video_woken and self.video.control_packets == 0 and time.monotonic() - self.last_video > 1:
                    video_woken = True
                    result = await asyncio.to_thread(self.usb.wakeVideo)
                    self.callback.onDiagnostic(f'QuickTime endpoint wake: {result}')
                    try:
                        await asyncio.to_thread(self.write, 42, quicktime.chunk('ping', b'\0\0\0\0\1\0\0\0'))
                    except Exception as error:
                        self.callback.onDiagnostic(f'QuickTime initial ping: {error_detail(error)}')
                if controller.done() and not control_error_reported:
                    control_error_reported = True
                    try: controller.result()
                    except asyncio.CancelledError: pass
                    except Exception as error:
                        self.control_ready(0)
                        self.status('control', str(error) or type(error).__name__)
                        self.callback.onDiagnostic('Control traceback: ' + ''.join(traceback.format_exception(error))[-1800:])
                        logging.exception('Control channel failed', exc_info=error)
                if control_failure and not control_error_reported:
                    controller.cancel()
                    await asyncio.gather(controller, return_exceptions=True)
                    self.control_ready(0); self.shortcuts_ready([])
                    self.status('control', control_failure[0])
                    control_error_reported = True
                if self.video_frames == 0 and time.monotonic() - self.last_video > 25:
                    raise ConnectionError('投屏画面超时；请解锁 iPhone 后重新连接')
                if (self.first_video_at is not None
                        and time.monotonic() - self.first_video_at > 10
                        and not self.callback.hasRenderedVideo()):
                    raise ConnectionError('已收到视频流，但解码器未显示首帧；请查看诊断日志')
                await asyncio.sleep(0.1)
            if self.failure: raise ConnectionError(self.failure)
        finally:
            # In particular, abort DDI download workers when startup fails:
            # cancelling asyncio.to_thread alone does not stop its thread.
            self.stop.set()
            self.capacity = 0
            if controller:
                # Give the HID task a chance to release while USB is still open.
                controller.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(controller, 5)
            if video_stream is not None:
                video_stream.beginStop()
            with suppress(Exception): await asyncio.to_thread(self.video.stop)
            if video_stream is not None:
                await asyncio.to_thread(video_stream.close)
            with suppress(Exception): await asyncio.to_thread(mux.close_all)
            # Keep USB reads and ACKs alive through HID release and service
            # shutdown. Stopping them with the session would strand final XPC
            # writes behind a full TCP window.
            self.io_stop.set()
            try:
                await server.close()
            finally:
                await asyncio.to_thread(lambda: [t.join(2.5) for t in self.workers])


def run(usb, callback, root):
    runtime = Runtime(usb, callback, str(root))
    execute(runtime)


def run_wireless(callback, root, name, width=1920, height=1080, fps=60):
    from imirror.wireless import WirelessRuntime
    execute(WirelessRuntime(callback, str(root), str(name), int(width), int(height), int(fps)))


def execute(runtime):
    global _current
    callback = runtime.callback
    with _lock:
        if _current is not None: raise RuntimeError('已有一个投屏会话正在运行')
        _current = runtime
    try:
        asyncio.run(runtime.run())
    except Exception as error:
        callback.onStatus('error', error_detail(error))
    finally:
        runtime.stop.set()
        runtime.control_ready(0)
        with _lock: _current = None


def submit(raw):
    if _current:
        message = json.loads(str(raw))
        _current.submit(message)


def device(raw):
    current = _current
    if current and hasattr(current, 'device'):
        current.device(raw)


def stop(session_id=None):
    current = _current
    if current:
        if session_id is not None:
            identify = getattr(current.callback, 'sessionId', None)
            if identify is None or identify() != session_id: return
        current.submit({'cancel': True})
        current.stop.set()
