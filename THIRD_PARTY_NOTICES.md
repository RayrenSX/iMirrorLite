# Third-party notices

This development project includes or adapts the following components. Each
component remains under its own license; this file does not relicense them.

| Component | Source | License / use |
| --- | --- | --- |
| iPhoneMirror Core | https://github.com/RayrenSX/iPhoneMirror | GPL-3.0; QuickTime/CoreMedia protocol and CoreDevice five-contact HID report port (`tools/usb_touch_bridge.py`, `docs/FIVE_POINT_TOUCH.md`); license in `licenses/iPhoneMirror-GPL-3.0.txt` |
| iUsbBridge | https://github.com/RayrenSX/iUsbBridge | Copyright 2026 RayrenSX; iUsbBridge Non-Commercial License in `licenses/iUsbBridge.txt`; adapted USB mux transport in `imirror/mux.py` |
| pymobiledevice3 11.3.1 | https://github.com/doronz88/pymobiledevice3 | GPL-3.0; Lockdown, RemoteXPC, userspace tunnel helpers; USB CoreDevice and DDI/TSS protocol adaptations |
| pmd-pytcp / pmd-net-addr / pmd-net-proto | Pinned packages in `requirements-android.txt` | Upstream package licenses; pure Python userspace network stack |
| Chaquopy 17.0 | https://github.com/chaquo/chaquopy | MIT; Android Python runtime integration |
| Python 3.13 | https://www.python.org/ | PSF License |
| cryptography 42.0.8 / CFFI | https://cryptography.io/ | Apache-2.0 / BSD; upstream native runtime dependencies retain their licenses |
| quicktime_video_hack test captures | https://github.com/danielpaulus/quicktime_video_hack | Copyright 2019 danielpaulus, MIT; original license retained in `tests/fixtures/LICENSE` |
| UxPlay / PlayFair | https://github.com/FDH2/UxPlay | Pinned commit `aec205d49302df8d4eb291b9e927ed428b2d0166`; GPL-3.0 project license in `third_party/uxplay/LICENSE`; original PlayFair C sources retained, AirPlay handlers and pairing adapted in `imirror/airplay.py`, FairPlay reply table in `imirror/fairplay.bin` |
| srptools 1.0.1 / opack2 0.0.1 | Pinned packages in `requirements-android.txt` | MIT; SRP and OPACK encoding for RemotePairing |

The remaining Python dependencies are pinned in
`app/src/main/python/requirements-android.txt`; their license metadata travels
with the dependency wheels. Local source provenance is recorded in
`third_party/provenance.json`.

The APK bundles Apple's generic personalized Developer Disk Image, BuildManifest
and trust cache, sourced from https://github.com/doronz88/DeveloperDiskImage at
commit `6eae353ae694bda1c421d4a3eee5459ae59c99a1` (build `27A5228h`). These Apple
assets retain their original ownership and are not relicensed by this project.
Hashes and source provenance are in `imirror/bundled_ddi/provenance.json`.
The application still requests a device-specific ticket from Apple's TSS before
uploading and mounting the generic image; no device-specific tickets are bundled.

`imirror/apple_root_ca.pem` contains Apple's publicly distributed Apple Root CA
certificate, obtained over verified HTTPS from
https://www.apple.com/appleca/AppleIncRootCertificate.cer. Its DER SHA-256 is
`b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024`.
It is used only to validate the `gs.apple.com` TSS endpoint, including hostname
validation; it does not change the app's general HTTPS trust store.
