# iMirror Lite for Android

安卓手机 / 平板通过 USB 或同一 Wi-Fi 直接显示并控制 iPhone 的开发测试版。运行时不使用电脑中转，APK 内包含 USB 协议、配对、用户态 CoreDevice 隧道和视频硬件解码。

**USB 投屏已在 EBG-AN00 / Android 12 与 iPhone 12 mini / iOS 18.7.8 上出画面，当前短排程版稳定期实测约 59.1 fps、重连后约 58.7 fps，用户确认延迟明显降低。** 仍有少量掉帧，操作期间窗口帧率也会波动，当前为开发测试版。多指触控已移植 iPhoneMirror 的 CoreDevice 五触点格式；安卓直连 12 mini 时，iPhone Safari 实际收到五个独立触点，用户确认手势正常、无跳点或残留。其他设备与长时间稳定性仍待验证。

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

2026-10-11 真机记录：

- EBG-AN00 / Android 12（API 31）/ ARM64：APK 安装、启动、打包运行时、真实 H.264 解码与 SurfaceView / PixelCopy 红色像素检查通过；修复了海思解码器不支持低延迟选项导致的配置失败。
- Android 15（API 35）/ x86_64 模拟器：此前刷新回调版的打包运行时、Apple TSS 根证书加载、USB JNI、Java 视频拆包、解码器关闭后重建，以及 H.264 → SurfaceView → PixelCopy 红色像素验证通过。当前短排程版以 ARM64 真机运行时与投屏实测为依据。
- iPhone 12 mini / iOS 18.7.8：通过电脑进行协议探测，已开启开发者模式、已挂载 Personalized DDI；APK 共用的用户态隧道与直接 HID 初始化代码可打开实际 mainTouchscreen 通道。媒体认证实际返回 9021，已加入对应处理。
- 此 iPhone 返回的触控属性不含 ReportDescriptor；当前版本识别其 CoreDevice touchscreen 服务并使用从 iPhoneMirror 移植的固定五指格式。安卓端注入 MotionEvent，经 USB 直达 iPhone，Safari TouchEvent 独立记录确认五指保持、单指移动、部分抬起、第六指拒绝、空槽复用和取消归零；用户实际操作确认无跳点或残留。见 [多指移植与真机验收](docs/multitouch-2026-10-11.md)。
- 安卓直接连接另一台 iPhone18,3 / iOS 26.6.2：USB 枚举、安卓内配对、CoreDevice 隧道、直接 HID 通道成功，用户确认单指控制正常。该组合的视频端点尚未返回有效 QuickTime 握手，未出画面；新增底层 USB 错误诊断等待真机复测。
- 安卓直连 iPhone 12 mini / iOS 18.7.8：真实 1082×2340 视频，当前短排程版系统实测首段 58.41 fps、45 秒稳定期 59.10 fps、重连后 58.74 fps；收到完整帧至显示平均约 54–55 ms，不含 iPhone 编码和 USB 接收前的时间。用户确认延迟明显降低。退出释放握手和重连通过，仍有掉帧及操作期间波动。首次 Apple TSS 签名、镜像 USB 上传和挂载也已在安卓端成功。见 [帧率与延迟真机记录](docs/video-60fps-2026-10-11.md)。
- 快捷操作已在安卓直连 12 mini 上测试，用户确认除 Dock 无用外，其余主页、多任务、面板、Siri、音量和锁屏正常；Dock 已移除。底部按键支持分别配置，默认左侧不发送操作。
- 三键导航版自定义保存、重启保留、横竖屏布局和重连已验证；最后 30 秒系统采样约 59.81 fps。见 [快捷操作与导航验收](docs/shortcuts-2026-10-11.md)。
- 协议与生命周期回归现为 51 项，包含真实握手采样、HID 编码、FIN 尾部数据、缓冲窗口、乱序重叠、畸形报文、重复版本响应、设备入站 magic、视频停止释放握手、退出时保留 USB 读取、开发者镜像个性化规则、下载备用源与校验、TSS 根证书验证，以及快捷键失败/取消释放、双主页、设备键盘能力、输入取消队列、CoreDevice 五指解码和发送失败/取消后的触点状态保留。

全链路审查与待解决项见 [docs/chain-review-2026-10-11.md](docs/chain-review-2026-10-11.md)。无线投屏和无线控制现已接入 APK，协议、构建与真机运行时检查记录见 [无线连接说明](docs/wireless-2026-10-13.md)。无线 ADB 仅用于开发调试。

2026-10-14 新一轮[上游对照与全流程静态修复](docs/chain-review-2026-10-14.md)包含 USB 视频/控制独立启动、严格递增视频时间戳、会话回调隔离、首次无线配对与触控并行、PIN 注册公钥检查、NSD 服务丢失处理及关闭资源寿命修复。按用户要求，该轮只编译打包，未运行测试；此前真机和回归结果属于当时版本。

K60 的后续 v4 用户日志显示 USB 11 个窗口平均接收 58.21 fps、显示回调 57.85 fps，无线五指控制通道也已成功建立。针对“比电脑仍不够流畅”的反馈，v5 加入有界无线接收/解码队列和发送端时间戳呈现，约 32 ms 小缓冲用于平滑抖动；v5 仅编译打包，效果待确认。见[帧率反馈与修订](docs/video-cadence-2026-10-14.md)。

v6 的「画面设置」分别保存有线/无线的分辨率、24/30/60/120 fps 和解码器。有线只改变本地 Surface 和显示输出，保持原始接收及参考帧解码；无线在连接前协商所选画布和帧率，应用后重启接收器并重新选择屏幕镜像。85 项回归、海思与高通安卓运行检查通过，海思 720p/30 fps 真实无线已出画面。设置语义、审查及各阶段验证边界见[分辨率帧率解码器设置](docs/video-settings-2026-10-14.md)。

v6.2 修复软件解码依赖默认硬解先创建、USB 合并收包造成额外丢帧及普通 USB 路径未严格限制 60 fps 的问题。四项 Java 检查、85 项 Python 回归和 APK / lint 构建通过；交付包为 `artifacts/iMirrorLite-DDI-v6.2-review-fixes-2026-10-14.apk`。本轮未恢复有线实测。

v6.3 修复运行中修改 USB 设置导致呈现时间倒退，以及合法无线高分辨率大帧被 2 MiB 限制拒绝的问题。完成三轮检查，93 项 Python 回归、Java 排程及回退检查、APK / lint 构建通过；交付包为 `artifacts/iMirrorLite-DDI-v6.3-final-review-2026-10-14.apk`。最终复查未发现本轮范围内剩余的可确认缺陷，详见[设置修复与验证记录](docs/video-settings-2026-10-14.md)。

之后完成了[无线实测与控制发现修复](docs/wireless-live-2026-10-14.md)：真实 iPhone 无线画面与重连通过，控制配对记录已保存，修复 IPv6 投屏身份与 IPv4 控制服务的关联。本地 83 项回归、APK / lint 构建和 EBG-AN00 当前版 `RUNTIME_SMOKE_OK` 通过；修复后的真实无线控制尚未验收。

同日随后完成[两台安卓无线 / 有线复测](docs/dual-device-live-2026-10-14.md)：当前 v6.3 在海思及 vivo 上均通过 `RUNTIME_SMOKE_OK`。海思 USB 安装开发者镜像后，无线视频和触控通过；有线真实 1206×2622 输入按保存的设置输出 442×960 / 30 fps，应用内重连通过，系统采样约 30.02 fps。vivo 保存首次 USB 配对后，无线 498×1080 视频和五指通道 / 用户触控通过，运动窗口显示回调约 59 fps，其他窗口仍有波动。用户后续确认重启 iOS 后 vivo 有线连接正常。该轮未修改生产代码或 APK，未验收长时间稳定性。

电脑只用于开发探测与安装，日常使用可通过局域网无线连接；USB 首次设置仍需将安卓作为 OTG 主机连接 iPhone。源码中的 `scripts/probe-iphone-hid.py` 只读取已信任 iPhone 的服务和触控属性，不发送触控。日志读取脚本见 `scripts/collect-android-diagnostics.ps1`。

真机验收需要检查：首次信任、视频首帧、单指拖动、双指缩放与旋转、五指独立事件、部分手指先抬起、iPhone 旋转、安卓旋转、断线重连、退出后无残留触点。记录安卓型号 / 系统版本、iPhone 型号 / iOS 版本及页面显示的触控能力。

第三方来源及许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
