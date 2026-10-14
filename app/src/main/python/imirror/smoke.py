"""Offline on-device verification; does not enumerate or control a phone."""
def check(root):
    from imirror.platform import configure
    configure(root)
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.userspace_tunnel import UserspaceTun, UserspaceDialPlane
    from imirror.usb_tunnel import UdpReceiver
    from pymobiledevice3.remote.core_device.hid_service import UniversalHIDServiceService
    from pymobiledevice3.remote.core_device.display_service import DisplayService
    from imirror import runtime, control, ddi, quicktime, hid
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from pymobiledevice3.ca import generate_pairing_cert_chain
    import asyncio
    import socket
    import ssl
    import tempfile
    from pathlib import Path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert key.key_size == 2048
    assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED
    apple_context = ssl.create_default_context(cafile=ddi.tss_ca(root))
    assert apple_context.check_hostname and apple_context.verify_mode == ssl.CERT_REQUIRED
    assert any(('commonName', 'Apple Root CA') in part
               for cert in apple_context.get_ca_certs() for part in cert['subject'])
    # Exercise fresh extraction from the actual APK's Chaquopy resources;
    # using the app's existing DDI cache would hide missing packaged assets.
    with tempfile.TemporaryDirectory(dir=root) as fresh:
        cache = ddi.download(fresh, lambda message: None, lambda: False)
        assert all(ddi.file_sha256(cache / name) == digest
                   for name, digest in ddi.BUNDLED_SHA256.items())
    print('BUNDLED_DDI_OK: packaged image, manifest and trust cache verified without download')
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.PKCS1)
    generate_pairing_cert_chain(public)

    async def check_stack():
        tun = UserspaceTun('imirror-smoke')
        tun.addr = 'fd00::1'
        tun.mtu = 16000
        try:
            await tun.up()
            tun.set_peer('fd00::2')
            async with UserspaceDialPlane(tun, 'fd00::2'):
                udp = UdpReceiver(tun.addr)
                assert udp.port > 0
                udp.close()
        finally:
            await tun.close()
    asyncio.run(check_stack())
    from imirror.wireless_smoke import check as check_wireless
    result = check_wireless(root)
    assert result.startswith('wireless_ok'), result
    print('WIRELESS_RUNTIME_OK: AirPlay PIN across TCP sockets, signed pairing, native PlayFair, encrypted video, TLS-PSK, userspace tunnel')
    return 'ok'


def video_protocol():
    from imirror.quicktime import Session
    return Session(lambda data: None, lambda *args: None)
