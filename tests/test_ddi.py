"""Bundled DDI integrity, offline preparation and cancellation recovery."""
import json
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/src/main/python'))
from imirror import ddi


class BundledDdiTests(unittest.TestCase):
    def test_fresh_cache_prepares_image_without_network(self):
        with tempfile.TemporaryDirectory() as root, patch('requests.get', side_effect=AssertionError('Network')):
            messages = []
            cache = ddi.download(root, messages.append, lambda: False)
            saved = json.loads((cache / 'verified.json').read_text())
            self.assertEqual(saved['commit'], ddi.BUNDLED_COMMIT)
            self.assertEqual(saved['sha256'], ddi.BUNDLED_SHA256)
            for name, expected in ddi.BUNDLED_SHA256.items():
                self.assertEqual(ddi.file_sha256(cache / name), expected)
            manifest = plistlib.loads((cache / 'BuildManifest.plist').read_bytes())
            self.assertTrue(manifest['BuildIdentities'])
            self.assertIn('无需下载', messages[0])

    def test_valid_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ddi.download(root, lambda s: None, lambda: False)
            with patch.object(ddi, 'extract_bundled_image', side_effect=AssertionError('Re-extraction')):
                self.assertEqual(ddi.download(root, lambda s: None, lambda: False), cache)

    def test_corrupt_cache_is_repaired_without_network(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ddi.download(root, lambda s: None, lambda: False)
            (cache / 'Image.dmg').write_bytes(b'corrupt')
            with patch('requests.get', side_effect=AssertionError('Network')):
                ddi.download(root, lambda s: None, lambda: False)
            self.assertEqual(ddi.file_sha256(cache / 'Image.dmg'), ddi.BUNDLED_SHA256['Image.dmg'])

    def test_self_reported_cache_hashes_do_not_override_pins(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ddi.download(root, lambda s: None, lambda: False)
            (cache / 'Image.dmg').write_bytes(b'other image')
            hashes = dict(ddi.BUNDLED_SHA256, **{'Image.dmg': ddi.file_sha256(cache / 'Image.dmg')})
            (cache / 'verified.json').write_text(json.dumps({'commit': ddi.BUNDLED_COMMIT, 'sha256': hashes}))
            ddi.download(root, lambda s: None, lambda: False)
            self.assertEqual(ddi.file_sha256(cache / 'Image.dmg'), ddi.BUNDLED_SHA256['Image.dmg'])

    def test_old_bundle_marker_is_updated(self):
        with tempfile.TemporaryDirectory() as root:
            cache = ddi.download(root, lambda s: None, lambda: False)
            (cache / 'verified.json').write_text(json.dumps({'commit': 'old', 'sha256': ddi.BUNDLED_SHA256}))
            ddi.download(root, lambda s: None, lambda: False)
            self.assertEqual(json.loads((cache / 'verified.json').read_text())['commit'], ddi.BUNDLED_COMMIT)

    def test_cancelled_extraction_cleans_partial_files_and_recovers(self):
        calls = 0
        def stopped():
            nonlocal calls
            calls += 1
            return calls >= 3
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, '连接已取消'):
                ddi.download(root, lambda s: None, stopped)
            cache = Path(root) / 'developer-image'
            self.assertFalse((cache / 'verified.json').exists())
            self.assertFalse(list(cache.glob('*.part')))
            ddi.download(root, lambda s: None, lambda: False)
            self.assertTrue((cache / 'verified.json').exists())

    def test_corrupt_packaged_image_never_becomes_verified(self):
        with tempfile.TemporaryDirectory() as package, tempfile.TemporaryDirectory() as root:
            bundle = Path(package) / 'bundled_ddi'
            bundle.mkdir()
            (bundle / 'Image.dmg').write_bytes(b'bad bundle')
            with patch('importlib.resources.files', return_value=Path(package)):
                with self.assertRaisesRegex(ValueError, '完整性校验失败'):
                    ddi.download(root, lambda s: None, lambda: False)
            cache = Path(root) / 'developer-image'
            self.assertFalse((cache / 'verified.json').exists())
            self.assertFalse((cache / 'Image.dmg').exists())
            self.assertFalse(list(cache.glob('*.part')))


if __name__ == '__main__': unittest.main()
