"""App-private storage and socket operations: no root or system daemon."""
import socket
import sys
import asyncio
from datetime import datetime
from pathlib import Path


def configure(root):
    from pymobiledevice3.osu.os_utils import OsUtils

    class Android(OsUtils):
        _os_name = 'android'
        is_admin = False
        bonjour_timeout = 3
        loopback_header = b''
        supports_unix_sockets = hasattr(asyncio, 'start_unix_server')
        access_denied_error = 'Permission denied'

        @property
        def usbmux_address(self):
            import os
            address = os.environ['USBMUXD_SOCKET_ADDRESS']
            if ':' not in address:
                return address, socket.AF_UNIX
            host, port = address.rsplit(':', 1)
            return (host, int(port)), socket.AF_INET

        @property
        def pair_record_path(self):
            return Path(root) / 'pairing'

        def get_home_folder_path(self):
            path = Path(root) / 'pairing'
            path.mkdir(parents=True, exist_ok=True)
            return path

        def get_homedir(self):
            return Path(root)

        def chown_to_non_sudo_if_needed(self, path):
            pass  # Android's app sandbox owns these files already.

        def set_keepalive(self, sock, after_idle_sec=3, interval_sec=3, max_fails=3):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        def get_ipv6_ips(self):
            return []  # This build discovers devices through UsbManager.

        def parse_timestamp(self, stamp):
            return datetime.fromtimestamp(stamp)

    OsUtils._instance = Android()
    from imirror import usb_tunnel
    # The upstream aggregate imports QUIC and privileged kernel TUN backends.
    # Supply the concrete USB-only implementation before importing its pure
    # userspace packet stack. No global socket APIs are patched.
    sys.modules['pymobiledevice3.remote.tunnel_service'] = usb_tunnel
