# 分辨率 帧率和解码器设置

本轮按用户要求参考原 iPhoneMirror，实现安卓的分辨率、帧率和解码器设置，并审查接收、解码、渲染、旋转、保存、重连和取消流程。用户随后明确允许测试，已恢复本地回归和两台安卓的运行检查。

## 设置入口与行为

主页面状态卡上的「画面设置」及预览左上角的「设置」都可打开配置。分别选择「有线 · 本地渲染」或「无线 · AirPlay 协商」，两套参数独立保存在应用私有 SharedPreferences。

| 设置 | 有线 | 无线 |
| --- | --- | --- |
| 分辨率 | 原始，540p、720p、1080p、1440p、2160p、2880p 的本地渲染上限 | 540p、720p、1080p、1440p、2160p、2880p 的 AirPlay 协商画布 |
| 帧率 | 24、30、60、120 fps 显示上限 | 在连接前向 iPhone 请求 24、30、60、120 fps，显示也遵守该上限 |
| 解码器 | 自动优先低延迟硬解、软件兼容模式、当前安卓提供的具体 AVC/HEVC 解码器 | 同一选择机制，接收端本地解码，不属于发送端编码器设置 |
| 修改后 | 分辨率和帧率立即更新本地 Surface / 已解码输出选择；更换解码器重新连接 | 停止当前会话、关闭原广播与接收端，按新参数重新启动；需要在 iPhone 重新选择屏幕镜像 |

有线的 QuickTime HPD1、NEED、原始压缩视频、配置数据与参考帧解码不因渲染设置而改动。分辨率通过 Surface 固定输出尺寸和 MediaCodec 的按比例缩放控制，UI 始终以原视频比例布局；触控使用实际可见视频矩形中的归一化坐标。原始画面小于上限时不放大，横竖屏切换保持同一宽高上限。

有线降低显示帧率只丢弃已解码画面，保留所有压缩参考帧。120 fps 是上限，源视频只有 60 fps 时不会制造额外帧。屏幕刷新率提示是可选请求，系统和驱动可能使用不同刷新率。

无线把所选宽、高、`widthPixels`、`heightPixels` 和 `maxFPS` 放入 `/info` 的 displays 能力，`refreshRate` 使用 UxPlay 的秒/帧表达 `1/fps`。实际分辨率以 iPhone 返回的配置为准，竖屏通常是低于画布宽度、具有手机原始比例的尺寸；选中的数字不是强迫手机产生该固定尺寸或帧率。

## 原 iPhoneMirror 对照

实际读取用户原工程 `C:/Users/Ray/Documents/iphoneMirror`：

- `src/App/ViewModels/MainViewModel.cs` 的 ResolutionPresets 明确只限制有线本地输出，USB 视频与 HPD1 不变；原有帧率选择为 24、30、60、120。
- `src/App/Models/DeviceVideoPreferences.cs` 的保存字段为渲染宽高、帧率、解码偏好。
- `src/UxPlayHost/UxPlayHost.cpp` 使用 `-s widthxheight@fps` 和 `-fps` 提交无线协商。
- `third_party/airplay-server/patches/Apply-DisplayCapabilityPatch.ps1` 写入 `/info` 的显示宽高及帧率；本项目使用已经固定的 UxPlay `raop_handlers.h` 确认 `refreshRate=1/fps` 的类型和含义。

该参考工程没有被修改，也未将它的桌面驱动或视频中转接入安卓。

## 完整审查结论

- 持久化数据校验：损坏、越界或类型错误的设置回到原始/1080p、60 fps、自动解码。取消对话框不写设置，编辑未使用的连接类型不影响当前会话。
- 每次连接保存自己的参数快照，Python 无线接收器在开放端口和回应 `/info` 前得到请求参数。
- 有线本地调整不重启 USB/协议，软件与硬件解码都保留参考链；解码选择变化走已有正常断开/重连流程，释放触点、旧 codec、旧 USB 连接及无线广播。
- 当前类型的无线设置变化自动重启接收器，应用退出、手动取消或 Surface 销毁会清除待重连请求，防止后台意外重启。
- 指定实例不可用或不支持收到的 MIME 时回到自动；替代实例创建、配置/启动失败时重建默认解码器。诊断同时记录请求名称与实际 codec，避免把回退记成请求生效。
- 30 fps 实测暴露了过紧的无线呈现时间窗口：原 50 ms 常无法容纳 30 fps 的两帧，并且迟到输出被连续丢弃。现将未来窗口设为至少两个目标帧周期，并将迟到画面安排到下一个有界时间槽。新增抖动回归要求 30 fps 在 0–60 ms 延迟波动中保留至少 90% 输出，修复后通过。
- 120 fps 选择同步影响有线及无线排程周期；没有把低显示帧率套到有线解码吞吐提示上。
- 主页面显示原始接收尺寸/计数和本地 Surface 请求尺寸/渲染回调计数，不能据此断言 SurfaceFlinger 每个帧都已实际扫描到屏幕。

## 本地与安卓检查

- 85 项 Python 回归通过，新增无线档位协商和参数拒绝；覆盖既有 USB、HID、DDI、PIN、地址关联、加密视频、TLS-PSK 及隧道。
- 纯 Java 检查通过原始尺寸、竖屏/横屏比例、禁止放大、24/30/60/120 显示上限、30 fps 抖动以及 120 fps 两条呈现调度。
- APK / 测试 APK / lint 构建成功，lint 0 errors / 21 warnings。
- 海思 EBG-AN00 / Android 12：`RUNTIME_SMOKE_OK`。高通 V2270A（SM7325）/ Android 15：`RUNTIME_SMOKE_OK`。两台覆盖自动硬解、软件解码、不存在的实例回退、关闭后重建、Surface 缩放、输入 90 帧且以 30 fps 上限显示、无线加密视频呈现和 PixelCopy 红色像素。高通该项显示 44 帧。
- vivo 禁止测试组件后台弹出，初次运行停在协议检查之后。本轮在 debug 组件上设置系统 DUMP 权限保护并由 ADB 前台打开，测试 runner 等待其就绪；release 不含该组件。没有放宽正常 App 的后台弹出权限。
- 最初合成无线发送端一次发送十二帧，仅覆盖硬件冷启动/过载且曾未显示。改为真实 60 fps 节奏后通过；没有把该失败标为已通过的初版证据。
- 海思界面保存了 USB 540p/30 fps 和无线 720p/30 fps，两套文件字段独立；重启/覆盖安装后保留。

记录：`artifacts/tests-video-settings-v6-final.log`、`artifacts/build-video-settings-v6-final.log`、`artifacts/build-v6-foreground-host.log`、`artifacts/runtime-video-settings-v6-final-hisi.log`、`artifacts/runtime-video-settings-v6-vivo-final-direct.log`、`artifacts/video-settings-vivo-runtime-markers.log`。

## 正在进行的 iPhone 实测

海思真实无线接收收到 332×720 视频，日志发送节奏约 30 fps，与 1280×720@30 请求对应；用户确认已出画面，五指控制通道建立。早期调度的实测丢帧触发上述修复，保留原记录 `artifacts/video-settings-720p30-live-before.log`。

vivo 的 1920×1080@60 接收端已接受真实 iPhone，实际收到 498×1080 视频，用户确认「vivo 已出画面」。日志存在接收/显示一致的约 49–59 fps 窗口；也有低频、网络积压和呈现丢帧窗口，不能记成稳定满 60 fps。10 秒 SurfaceFlinger 采样收集 346 次实际呈现，整体约 34.33 fps，中位间隔约 16.67 ms，最大间隔约 2.33 秒；未控制同一画面持续运动，不能将静止/暂停与网络/处理掉帧归为一种原因。

vivo 测量脚本适配了 Android 15 厂商输出的 `RequestedLayerState{...}` 包装，仍严格只选择本应用的一个 BLAST 视频层。实测记录为 `artifacts/video-settings-vivo-1080p60-live-final.log` 和 `artifacts/video-settings-vivo-1080p60-presentation.json`。

## 最终交付

`artifacts/iMirrorLite-DDI-v6-video-settings-2026-10-14.apk`，61,358,419 字节；SHA-256 为 `d13ebb22b2c07451358e5a576a4a953c1a645931dfff0b594c2f5982e062fe0b`。全部生产 Python 模块的已打包 code object 与当前源码一致，三个内置 DDI 文件的摘要匹配固定 provenance；沿用两个聊天的无线发现、配对、控制和 DDI 改动。

最终 lint 为 0 errors / 21 warnings。海思已安装呈现调度修订版并通过运行检查；vivo 已重新安装最终 debug APK 和测试 APK，前台测试完整通过，真实 iPhone 无线首帧和显示也已确认。有线原始流与渲染分离的组合实测尚待用户连接数据线，不能从合成运行检查推定为已实测通过。

## 海思有线复测

用户确认已把 iPhone 直连海思，并确认停止无线、解锁及重新插拔。系统初次仍为 `sink/device`，通过真实 USB 端口切为 `source/host` 后成功枚举 iPhone18,3 / iOS 26.6.2，选择 QuickTime 配置 5，建立五指直接 HID 与快捷通道。

初次与重新插拔后的两次连接均没有收到 PING；初始视频 OUT 返回 `-110`，约 25 秒后按已有超时流程退出，`encoded frames=0`。这重现了此前已记录的该组合 USB 视频兼容问题，不能解释为 540p/30 fps 限制失败或通过，因为没有视频到达解析/解码/渲染阶段。

再次与原 iPhoneMirror 的 libusb1/UsbDk 路径对照，补齐开始 URB 之前的视频 IN/OUT 端点 halt 清理，作为可失败回退的启动步骤，诊断记录 `startupVideoReset`。修订版构建和海思完整 `RUNTIME_SMOKE_OK` 通过；实际该连接 `startupVideoReset=ok`，视频仍无 PING、输出超时、帧数为 0，故不能声称此步骤修复了该组合。

记录：`artifacts/video-settings-usb-live-attempt.log`、`artifacts/video-settings-usb-live-replug.log`、`artifacts/video-settings-usb-startup-reset-live.log`、`artifacts/video-settings-usb-descriptors.txt`、`artifacts/build-usb-startup-reset-2026-10-14.log`、`artifacts/runtime-usb-startup-reset-hisi.log`。端点清理修订包另存为 `artifacts/iMirrorLite-DDI-v6.1-usb-startup-2026-10-14.apk`，不是该 iPhone USB 视频已通过验收的证明。

用户明确要求「暂不重启，先结束有线测试」。已结束这项真机测试，iPhone 未重启；海思测试会话已按超时退出并释放控制通道。结论保留为：iPhone18,3 / iOS 26.6.2 的 USB 视频未收到 PING，0 编码帧，有线 540p/30 fps 的组合实测未通过；已有无线画面和两台安卓的合成解码/缩放/帧率检查成绩保持其原适用范围。

## 审查修复 v6.2

针对随后审查确认的三项问题，修改正式解码器和 USB 输出排程：

- 软件及指定解码器先根据能力元数据选择，直接创建所选实例。自动解码器只在未指定、指定实例不可用或启动失败后进入；不再预先创建默认硬解。自动模式也先查询默认和低延迟候选的名称再创建。失败的实例先释放，回退失败保留原始异常。
- USB 不再用本地接收 PTS 做帧率门控。同一次读取里的多帧可以只相差 1 微秒，这不代表源帧间隔。各品牌的 USB 输出统一使用有界呈现时间槽，所有压缩帧仍完整进入解码器。
- 删除普通 USB 路径的 25 ms 时间戳截断，超出目标帧率的已解码输出直接丢弃。相邻排程时间至少相隔一个目标帧周期，60 fps 设置不会随 120 fps 输入变成 120 fps 提交。24/30 fps 的未来窗口允许至少两个周期（约 83.3/66.7 ms），60/120 fps 仍为 40 ms，以容纳短突发并限制积压。

新增纯 Java 回归覆盖：默认硬解不可用时所选软件实例仍可先成功、指定实例创建/启动失败后的自动回退及错误保留；USB 24/30/60/120 fps、逐帧和成批解码输出、每次读取合并 1/2/4 帧、120→60 fps 限制、停顿和重建。

四帧合并收包的 60 fps 输入，在 30 fps 设置下得到 10 秒 300 个呈现槽；同一次 drain 返回多帧时也保留 300 个。120→60 fps 检查要求每两个呈现槽之间至少相隔目标周期，允许在采样窗口结束后仍排有少量未来帧。大于延迟窗口的突发仍可能丢弃旧输出，不保证任意网络或 USB 积压都能显示全部源帧。

本轮四项 Java 检查及既有 85 项 Python 回归通过；APK、测试 APK 和 lint 构建通过，lint 为 0 errors / 21 warnings。APK 已核对包含新增回退类及当前 USB 排程代码，v2 签名验证通过。证据：`artifacts/tests-video-review-java-2026-10-14.log`、`artifacts/tests-video-review-python-2026-10-14.log`、`artifacts/build-video-review-2026-10-14.log`。

交付包为 `artifacts/iMirrorLite-DDI-v6.2-review-fixes-2026-10-14.apk`，61,358,419 字节；SHA-256：`f4d8b4db9267417bb5714fd7d45cd6c7e0bd1f86838b49cc75c299288336dd0e`。沿用内置 DDI 和已有 USB 启动端点清理。本轮没有安装到安卓或恢复有线实测，不能据本地排程回归断言此前无 PING、0 编码帧的 USB 兼容问题已经解决。

## 反复审查修复 v6.3

用户要求修复后反复审查。本轮修复后完成三轮检查：先对两个已确认问题补充失败回归并修复，再检查协议边界及取消/关闭，最后检查实际变更、完整回归、构建和交付内容。最后一轮没有发现本轮范围内剩余的可确认缺陷；这不是所有设备、驱动及运行情形均无错误的证明。

- USB 的 `setFrameRate` 更新周期时保留最后已提交的呈现时间，仅解码器关闭或重建时清空排程。运行中修改分辨率（帧率相同）或修改帧率，都不能让新帧排在已经交给 Surface 的旧帧之前。新增 24/30/60/120 的全部切换组合回归，同时确认目标间隔、队列边界、恢复呈现及新会话重置。
- AirPlay 的 RTSP 正文及配置仍限 2 MiB；视频帧按已验证的实际宽高使用 `min(32 MiB, max(2 MiB, width*height))` 上限，与安卓解码输入提示衔接。超限包在读取正文之前拒绝，未完成配置的帧也保持小上限。尺寸变化、竖屏、所有分辨率档位及最大尺寸均有回归。超限诊断在检查前记录本包种类、长度和标志，避免报告前一个包的数据。
- 取消检查确认关闭会等待正在执行的解码线程完成，之后再释放 owner 和发出断开通知；重复关闭没有残留任务。独立检查确认放宽视频不会放宽 RTSP 控制正文限制。

完整 Python 回归为 93 项，通过；四项 Java 检查通过，覆盖此前软件选择/回退、有线合并收包/显示限帧，以及本轮运行中修改选项。实际 3840×2160 H.264 样本的 4,963,984 字节帧，修复前因 2 MiB 上限被拒绝，修复后能通过正式接收器的解密、分帧与回调，输出与原 NAL 数据一致，关闭后没有待完成解码任务。该样本另经 FFmpeg 解码成功；这里没有验证安卓硬件对该 4K 样本的支持，也没有声称 iPhone 实际发送过它。

APK、测试 APK 和 lint 构建成功，lint 为 0 errors / 21 warnings。检查打包的 17 个生产 Python 模块与源码 code object 一致，三个内置 DDI 摘要匹配 provenance，APK v2 签名验证通过。证据：

- `artifacts/review-v6.3-java-round1.log`
- `artifacts/review-v6.3-video-round2.log`
- `artifacts/review-v6.3-real-4k-frame.log`
- `artifacts/tests-video-review-v6.3-final.log`
- `artifacts/build-video-review-v6.3-final.log`
- `artifacts/package-video-review-v6.3-final.log`

交付包：`artifacts/iMirrorLite-DDI-v6.3-final-review-2026-10-14.apk`，61,358,631 字节；SHA-256：`62491f04c1b6c51ec0f573b9bca782355028a6b2104b5dd2b34c2e88534dde69`。本轮未安装到安卓，也未恢复用户已结束的有线实测。此前 iPhone18,3 / iOS 26.6.2 无 PING、0 编码帧的 USB 视频兼容问题仍未实机解决；本轮两个修复均作用于接收/显示阶段，不能据此声称修好了那项握手问题。
