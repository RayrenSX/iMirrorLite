"""Personalized developer image setup over USB. No desktop prerequisite.

The APK bundles a generic image pinned to a doronz88/DeveloperDiskImage commit.
It is verified and extracted to app-private storage without a network download,
then personalized for the connected device by Apple's HTTPS TSS.
Protocol derived from pymobiledevice3 mobile_image_mounter.py and tss.py.
"""
import asyncio
import hashlib
import json
import plistlib
import threading
from pathlib import Path
import uuid

SERVICE = 'com.apple.mobile.mobile_image_mounter'
REPO = 'https://api.github.com/repos/doronz88/DeveloperDiskImage'
DIRECTORY = 'PersonalizedImages/Xcode_iOS_DDI_Personalized'
HEADERS = {'User-Agent': 'iMirrorLite/0.1'}
FILES = ('Image.dmg', 'BuildManifest.plist', 'Image.dmg.trustcache')
BUNDLED_COMMIT = '6eae353ae694bda1c421d4a3eee5459ae59c99a1'
BUNDLED_SHA256 = {
    'Image.dmg': '05fd807da5e19f030fa4941f24800c965c6c77982ab572dd5d1ef778fb69f9ca',
    'BuildManifest.plist': '8edd4a2f4f4ef1fbd7bfe49785d8badc673d1395d1d94d85b132ca8ab5ecaf54',
    'Image.dmg.trustcache': '36af60889ff5a737874a26daeb8e1a0139ebfebec6ec2e4d8f6a3c1bf1dce35c',
}
_signing_slots = threading.BoundedSemaphore(2)


def file_sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(root, status, stopped):
    """Prepare the bundled DDI; keep the existing USB setup entry point."""
    if stopped(): raise RuntimeError('连接已取消')
    cache = Path(root) / 'developer-image'
    cache.mkdir(exist_ok=True)
    metadata = cache / 'verified.json'
    if metadata.exists():
        try:
            saved = json.loads(metadata.read_text())
            if (isinstance(saved, dict) and saved.get('commit') == BUNDLED_COMMIT and saved.get('sha256') == BUNDLED_SHA256
                and all((cache / name).is_file() and file_sha256(cache / name)
                        == BUNDLED_SHA256[name] for name in FILES)):
                return cache
        except (ValueError, OSError, KeyError): pass
    return extract_bundled_image(cache, status, stopped)


def extract_bundled_image(cache, status, stopped):
    from importlib.resources import files
    resources = files('imirror').joinpath('bundled_ddi')
    status('正在准备内置开发者镜像，无需下载')
    for name in FILES:
        if stopped(): raise RuntimeError('连接已取消')
        path = cache / name
        if path.is_file() and file_sha256(path) == BUNDLED_SHA256[name]: continue
        temporary = cache / (name + '.part')
        digest = hashlib.sha256()
        try:
            with resources.joinpath(name).open('rb') as source, temporary.open('wb') as output:
                while block := source.read(256 * 1024):
                    if stopped(): raise RuntimeError('连接已取消')
                    output.write(block)
                    digest.update(block)
            if digest.hexdigest() != BUNDLED_SHA256[name]:
                raise ValueError('内置开发者镜像完整性校验失败')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if stopped(): raise RuntimeError('连接已取消')
    temporary = cache / 'verified.json.part'
    try:
        temporary.write_text(json.dumps({'commit': BUNDLED_COMMIT, 'sha256': BUNDLED_SHA256}))
        temporary.replace(cache / 'verified.json')
    finally:
        temporary.unlink(missing_ok=True)
    return cache


def download_file(cache, name, entry, commit, status, stopped):
    import requests
    expected = int(entry['size'])
    sources = (
        (f'https://raw.githubusercontent.com/doronz88/DeveloperDiskImage/{commit}/{DIRECTORY}/{name}', HEADERS),
        # The same immutable blob via GitHub's API, for networks which reset
        # raw.githubusercontent.com connections. Integrity checks remain identical.
        (f"{REPO}/git/blobs/{entry['sha']}", {**HEADERS, 'Accept': 'application/vnd.github.raw+json'}),
    )
    for index, (url, headers) in enumerate(sources):
        if stopped(): raise RuntimeError('连接已取消')
        git = hashlib.sha1(f'blob {expected}\0'.encode())
        sha = hashlib.sha256()
        temp = cache / (name + '.part')
        read, last_progress = 0, -1
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 30)) as response:
                response.raise_for_status()
                with temp.open('wb') as output:
                    for block in response.iter_content(1024 * 256):
                        if stopped(): raise RuntimeError('连接已取消')
                        read += len(block)
                        if read > expected: raise ValueError('开发者镜像下载超出预期大小')
                        output.write(block); git.update(block); sha.update(block)
                        percent = int(read * 100 / expected)
                        if percent // 10 != last_progress:
                            last_progress = percent // 10
                            status(f'下载开发者镜像 · {name} · {percent}%')
        except requests.RequestException:
            if index == len(sources)-1: raise
            status(f'原下载连接中断，切换 GitHub 校验源 · {name}')
            continue
        if read != expected or git.hexdigest() != entry['sha']:
            raise ValueError('开发者镜像完整性校验失败')
        temp.replace(cache / name)
        return sha.hexdigest()


def ticket_request(manifest, identifiers, ecid, nonce):
    board, chip = identifiers['BoardId'], identifiers['ChipID']
    identity = next((v for v in manifest['BuildIdentities']
                     if int(v['ApBoardID'], 0) == board and int(v['ApChipID'], 0) == chip), None)
    if identity is None: raise ValueError('开发者镜像不支持这台 iPhone 的芯片')
    request = {'@HostPlatformInfo': 'mac', '@VersionInfo': 'libauthinstall-1104.0.9',
               '@UUID': str(uuid.uuid4()).upper(), '@ApImg4Ticket': True, '@BBTicket': True,
               'ApBoardID': board, 'ApChipID': chip, 'ApECID': ecid, 'ApNonce': nonce,
               'ApProductionMode': True, 'ApSecurityDomain': 1, 'ApSecurityMode': True,
               'SepNonce': b'\0' * 20, 'UID_MODE': False}
    request.update({k: v for k, v in identifiers.items() if k.startswith('Ap,')})
    condition_values = {'ApRawProductionMode': True, 'ApCurrentProductionMode': True,
                        'ApRawSecurityMode': True, 'ApRequiresImage4': True}
    rules = identity['Manifest'].get('LoadableTrustCache', {}).get('Info', {}).get('RestoreRequestRules', [])
    for key, item in identity['Manifest'].items():
        if 'Info' not in item or not item.get('Trusted'): continue
        entry = {k: v for k, v in item.items() if k != 'Info'}
        for rule in rules:
            if all(k in condition_values and condition_values[k] == v for k, v in rule['Conditions'].items()):
                entry.update({k: v for k, v in rule['Actions'].items() if v != 255})
        if item.get('Digest') is None: entry['Digest'] = b''
        request[key] = entry
    return request


def tss_ca(root):
    from importlib.resources import files
    # TSS uses Apple's own CA, absent from Mozilla/certifi's public-web roots.
    # Use Apple's published root for this endpoint, with hostname validation.
    ca = Path(root) / 'apple-tss-ca.pem'
    ca.write_bytes(files('imirror').joinpath('apple_root_ca.pem').read_bytes())
    return str(ca)


def sign(request, root):
    import requests
    response = requests.post('https://gs.apple.com/TSS/controller?action=2',
        headers={'Content-Type': 'text/xml; charset=utf-8', 'User-Agent': 'InetURL/1.0'},
        data=plistlib.dumps(request), verify=tss_ca(root), timeout=(15, 60))
    response.raise_for_status()
    data = response.content
    if b'MESSAGE=SUCCESS' not in data or b'REQUEST_STRING=' not in data:
        raise RuntimeError('Apple 拒绝了开发者镜像个性化请求')
    return plistlib.loads(data.split(b'REQUEST_STRING=', 1)[1])['ApImg4Ticket']


async def sign_cancellable(request, root, stopped):
    """Cancel the session without waiting for requests' blocking TLS timeout.

    An in-flight HTTPS exchange may finish in its daemon thread, but it cannot
    upload/mount anything or deliver a ticket into a cancelled session.
    """
    if stopped(): raise RuntimeError('连接已取消')
    if not _signing_slots.acquire(blocking=False):
        raise RuntimeError('之前的 Apple 验证请求尚未结束，请稍后重新连接')
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    def deliver(ticket, error):
        if result.done(): return
        if error is not None: result.set_exception(error)
        elif stopped(): result.cancel()
        else: result.set_result(ticket)
    def worker():
        ticket = error = None
        try:
            if stopped(): raise RuntimeError('连接已取消')
            ticket = sign(request, root)
        except Exception as caught: error = caught
        finally: _signing_slots.release()
        try: loop.call_soon_threadsafe(deliver, ticket, error)
        except RuntimeError: pass  # Its session's event loop has already exited.
    try: threading.Thread(target=worker, name='ddi-tss', daemon=True).start()
    except BaseException:
        _signing_slots.release()
        raise
    return await result


async def ensure(lockdown, root, status, stopped):
    if stopped(): raise RuntimeError('连接已取消')
    if not await asyncio.wait_for(lockdown.get_developer_mode_status(), 10):
        raise RuntimeError('请在 iPhone「设置 → 隐私与安全性 → 开发者模式」开启并重启，然后重新连接')
    service = await asyncio.wait_for(lockdown.start_lockdown_service(SERVICE), 10)
    try:
        async def exchange(message):
            if stopped(): raise RuntimeError('连接已取消')
            return await asyncio.wait_for(service.send_recv_plist(message), 15)
        images = await exchange({'Command': 'CopyDevices'})
        if any(v.get('DiskImageType') == 'Personalized' for v in images.get('EntryList', [])):
            return
        lookup = await exchange({'Command': 'LookupImage', 'ImageType': 'Personalized'})
        if lookup.get('ImageSignature'): return
        cache = await asyncio.to_thread(download, root, status, stopped)
        identifiers = await exchange({'Command': 'QueryPersonalizationIdentifiers'})
        nonce = await exchange({'Command': 'QueryNonce', 'PersonalizedImageType': 'DeveloperDiskImage'})
        manifest = plistlib.loads((cache / 'BuildManifest.plist').read_bytes())
        request = ticket_request(manifest, identifiers['PersonalizationIdentifiers'],
                                 lockdown.ecid, nonce['PersonalizationNonce'])
        status('正在向 Apple 验证开发者镜像')
        ticket = await sign_cancellable(request, root, stopped)
        if stopped(): raise RuntimeError('连接已取消')
        image = cache / 'Image.dmg'
        response = await exchange({'Command': 'ReceiveBytes', 'ImageType': 'Personalized',
            'ImageSize': image.stat().st_size, 'ImageSignature': ticket})
        if response.get('Status') != 'ReceiveBytesAck': raise RuntimeError('iPhone 未接受开发者镜像传输')
        status('正在通过 USB 安装开发者镜像')
        with image.open('rb') as stream:
            while data := stream.read(1024 * 256):
                if stopped(): raise RuntimeError('连接已取消')
                await asyncio.wait_for(service.sendall(data), 15)
        if (await asyncio.wait_for(service.recv_plist(), 30)).get('Status') != 'Complete':
            raise RuntimeError('开发者镜像 USB 传输失败')
        response = await exchange({'Command': 'MountImage', 'ImageType': 'Personalized',
            'ImageSignature': ticket, 'ImageTrustCache': (cache / 'Image.dmg.trustcache').read_bytes()})
        if response.get('Status') != 'Complete':
            raise RuntimeError('开发者镜像挂载失败：' + str(response.get('DetailedError', response.get('Error', '未知原因'))))
    finally:
        await asyncio.wait_for(service.close(), 3)
