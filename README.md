# iPhoneMirror for Android

安卓手机 / 平板通过 USB 或同一 Wi-Fi 直接显示并控制 iPhone 的开发测试版。运行时不使用电脑中转，APK 内包含 USB 协议、配对、用户态 CoreDevice 隧道和视频硬件解码。
## 安装与连接

1. 从 [GitHub Releases](https://github.com/RayrenSX/iMirrorLite/releases) 下载并安装 APK（开发签名）；自行构建的安装包位于 `app/build/outputs/apk/debug/app-debug.apk`。支持 Android 9+、ARM64 / x86_64；安卓设备必须支持 USB Host / OTG。
2. 用可传输数据的数据线连接 iPhone，安卓作为 USB 主机。部分设备需要打开系统 OTG 开关或 OTG 转接头。
3. 保持 iPhone 解锁。在 APK 内点“连接 iPhone”，允许安卓访问 USB，并在 iPhone 上确认信任。投屏模式切换会重新枚举 USB，可能再次请求授权。
4. 反向控制要求 iOS 18+，并开启“设置 → 隐私与安全性 → 开发者模式”。APK 已内置开发者镜像，首次设置从内置资源校验并解压，无需下载镜像；仍需联网向 Apple 请求设备个性化验证。镜像缓存在应用私有目录。
5. 在预览区域触摸、拖动、双指操作。界面分别报告投屏状态和触控能力；“本机多指检测”只检查安卓触摸屏，不向 iPhone 发送任何输入。
6. 镜像下方三键默认是左侧未设置、中间主页、右侧多任务。三键与实际镜像同宽，紧贴画面下缘。长按任一键选择功能，设置会保存在本机，下次启动仍生效。左侧不模拟 iPhone 的“返回”操作。控制中心、通知中心、音量、Siri 和锁屏在操作栏中；横屏时操作栏移到右侧。
7. 点镜像右上角“全屏”，隐藏顶部、操作栏和安卓系统栏，按原比例尽可能放大画面并保留三键。点右下角收起图标或安卓返回键恢复普通界面，投屏连接持续保持。主页使用浅色与青绿配色，全屏使用纯黑背景；右上角问号打开“使用说明”，可查看诊断日志和本机多指检测。

外接键盘按住 **Ctrl + Alt**，再按 H / Tab / C / N / S / ↑ / ↓ / L，分别执行主页、多任务、控制中心、通知中心、Siri、音量加、音量减和锁屏。按住不重复执行。按键只在相应控制通道可用时发送；未连接时仍可长按配置底部三键。

现已接入无线 AirPlay 投屏与 RemotePairing 无线控制，可在「连接 iPhone」选择 USB 或无线。无线控制首次需通过 USB 保存无线配对与准备开发者环境，使用见 [无线连接说明](docs/wireless-2026-10-13.md)。当前 v6.3 已在 EBG-AN00 / Android 12 与 vivo V2270A / Android 15 上实测无线画面及触控通过，使用 iPhone18,3 / iOS 26.6.2；海思有线画面、触控和重连通过，用户后续确认重启 iOS 后 vivo 有线连接正常。详情见[两台安卓无线 / 有线复测](docs/dual-device-live-2026-10-14.md)。无线帧率仍有波动。尚未实现音频与后台投屏。离开应用会释放触点并断开。不同安卓 USB 控制器和 iOS / DDI 组合的兼容性仍需测试。

## 已实现的协议路径

```text
Android UsbManager
  ├─ QuickTime 0x2A → CoreMedia → H.264 / HEVC → MediaCodec → SurfaceView
  └─ usbmux 0xFE → 私有 Unix socket → Lockdown 配对
       → Personalized DDI → CoreDeviceProxy → 用户态 IPv6/TCP
       → RemoteXPC → 媒体认证 / 9021 时验证直接 HID → Universal HID

Android MotionEvent → 稳定 pointerId → CoreDevice 五触点 / HID 描述符 → 完整多触点报告
```

- 视频按比例居中显示，触控坐标只对应实际画面区域。
- 触点编号在移动与手指顺序变化时保持稳定，最多五个并行触点。
- 抬手、取消、布局变化、断连和会话结束均走触点释放路径。
- 系统按钮通过 Indigo HID 发送；多任务为两次主页按键。控制中心、通知中心复用设备已发布的键盘 surface 512，发送 Globe + C / N。没有设备键盘时仅在媒体认证成功后尝试独立注册，避免直接模式下注册失败破坏触控。按下失败、取消及退出时也尝试释放按键。
- 输入发送超时不重放。输入积压触发取消，避免遗失抬手事件。
- 配对记录仅保存在 Android 应用私有目录，禁止系统备份；内部 usbmux 服务只使用私有 Unix socket。
- “使用说明 → 诊断日志”可查看并复制连接状态。日志保存在应用私有目录，不记录视频、触摸坐标或配对证书；换回电脑后可读取日志排查 OTG 问题。
- 内置开发者镜像固定到 Git commit `6eae353ae694bda1c421d4a3eee5459ae59c99a1`（构建 `27A5228h`），解压与缓存复用均按固定 SHA-256 校验；Apple 验证使用 HTTPS 和证书校验。

`iUsbBridge` 原有 `0xC2 | slot` 写法在 slot 0/2、1/3 上产生重复值。本项目移植 iPhoneMirror 已校正的 58 字节 CoreDevice 格式：按下为 `0xC0 | slot`，抬起为 `slot`，身份表按槽位索引，时间戳位于偏移 45。完整快照保留静止手指，空槽仅在释放发送成功后复用，静止触点每秒保活。可解析的并行 HID 描述符仍优先使用；只有服务 257 且 Product 为 CoreDevice touchscreen 的已知触控面才使用固定五指格式，未知触控面保留单指回退。

## 构建

依赖：JDK 17、Android SDK Platform 35、NDK 27.0.12077973、CMake 3.22.1、Gradle 8.9、Python 3.13（构建 Chaquopy 字节码）、首次构建联网下载依赖。

```powershell
./scripts/build-android.ps1 -Tasks ':app:assembleDebug'
```

脚本优先使用本仓库 `.tools` 的便携工具链，也支持 `-JavaHome`、`-AndroidSdk`、`-GradlePath`。不修改全局环境变量。其他系统安装对应依赖后运行 `./gradlew :app:assembleDebug`。

## 验证

```powershell
python -m unittest discover -s tests -v
./scripts/build-android.ps1 -Tasks ':app:assembleDebug',':app:assembleDebugAndroidTest'
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb shell am instrument -w com.rayren.imirrorlite.test/com.rayren.imirrorlite.RuntimeSmokeTest
```

打包运行时检查会在安卓内实际加载 Python、原生密码库与 USB JNI、生成配对证书、启动 / 关闭用户态 TCP 栈，再用正式解码器把 H.264 测试图显示到 SurfaceView，通过 PixelCopy 检查像素，并用 Android MotionEvent 验证五指路由、指序变化、第六指拒绝与取消。成功标志为 `RUNTIME_SMOKE_OK`，不连接 iPhone；测试会临时打开应用页面。

第三方来源及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
