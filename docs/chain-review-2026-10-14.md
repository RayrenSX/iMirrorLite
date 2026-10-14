# 类似项目对照与全流程静态修复

本轮按用户要求查阅上游项目，审查 USB 和无线两条连接的初始化、视频、触控、DDI、断开与重连，直接修改源码。**不执行单元测试、运行时测试、设备安装或真机操作。** 编译打包只生成交付 APK，不代表运行或性能验证。

## 查阅的上游实现

本轮通过 GitHub API 实际读取以下源码，保留所读文件的 Git blob SHA，原始文件在 `artifacts/upstream-review-2026-10-14/`。

| 项目 | 对照内容 | 文件与 Git blob SHA |
| --- | --- | --- |
| [Moonlight Android](https://github.com/moonlight-stream/moonlight-android) | 硬件解码器选择、低延迟参数、输出缓冲、SPS 改写条件 | `MediaCodecHelper.java`：`24d1f01ab0dc407431984161dd36b46033d5abf0`；`MediaCodecDecoderRenderer.java`：`5d4e44ecfbb34c8150b419dd07ce76429c432985` |
| [scrcpy](https://github.com/Genymobile/scrcpy) | 完整参考帧解码、显示端背压和最新画面策略 | `app/src/decoder.c`：`f0ba7aca6d2c12af44b5f7af47e55cb822fb3dcb`；`app/src/screen.c`：`fec823e73e94773b801176914fd07fab7fa0facc` |
| [UxPlay](https://github.com/FDH2/UxPlay) | AirPlay PIN/SRP、签名配对、注册公钥检查 | 在线 `lib/pairing.c`：`c38f191cc31b1e59d75922d096b5d67d99c06890`；另读仓库原有固定版本 `lib/raop_handlers.h` |
| [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) | Personalized DDI、挂载与控制服务 | 在线 `mobile_image_mounter.py`：`a2241aab35bbfba878da620f1cba5f2414a08578`；本项目移植的 RemotePairing、CoreDevice 服务仍以固定 11.3.1 为基础 |
| [Linux usbfs](https://github.com/torvalds/linux/blob/master/drivers/usb/core/devio.c) | URB 取消、拔线后完成回收、fd 关闭前的资源寿命 | 在线读取 `devio.c`，用于核对异步 USB 回收语义 |

这些项目不是本应用真机兼容性的证明。Moonlight 的 SPS 改写依赖已知编码参考帧条件；本轮没有把它的 `num_ref_frames` 或 `num_reorder_frames` 修改直接套到未知 iPhone 流上。

## 逐段审查与修复

### USB 枚举与会话

检查授权、设备选择、序列号、QuickTime 重新枚举、配置/接口切换、native URB、收发超时、停止握手和 fd 关闭顺序。保留既有序列号规范化、同设备匹配、授权请求编号和不重放不确定写入的行为。

- 旧 USB 会话要先等待控制握手成功才能进入视频监控；控制超时会结束已开始的视频。改为独立控制初始化任务，控制失败显示在控制状态，视频继续收帧、监控和退出。
- mux 收包/解析异常改为关闭控制连接并唤醒等待者，不再把独立视频会话一同停止。
- UsbHost 关闭前等待全部进行中的 USB 调用结束，避免只靠读取线程的定时 join 就关闭 JNI 使用的 fd。新调用在开始关闭后直接拒绝。
- 视频 IN 端点 stall 清除后继续读取；不确定的 OUT 写入仍报错且不重试。
- USB 激活线程和迟到的轮询检查原授权请求编号，取消或新尝试后不再调度旧激活流程。
- 本地 usbmux 转接在 Connect 响应阶段被取消时也回收已建立连接；关闭 socket 的等待增加边界。

### 视频解析与显示

检查 QuickTime 长度、NEED 与解析顺序、CoreMedia 字段、AVC/HEVC 配置、NAL 转换、Surface 生命周期、硬件解码参数、输出提交与诊断。

- 继续完整解码参考帧，仅在解码输出阶段处理背压。高通保留最多两个输出并安排有界显示时间，沿用前一轮已经加入的厂商参数分级回退。
- 同一次 USB 读取中的多个采样不再共用相同 PTS；给每个完整帧分配严格递增的本地接收时间戳，避免驱动合并/混淆相同时间戳输出。
- Java 视频组包缓存设置上限，防止异常分片积累内存。
- 解码器创建、配置或启动最终失败时，立即释放未就绪实例。厂商能力查询异常退回常规路径；Surface 刷新率提示失败不终止视频连接。
- 不把输出的 codec-config 缓冲计入普通画面或送去呈现。
- 重复尺寸更新只安排一次 UI 布局任务，减少高帧率启动/旋转期间的布局和触点取消。
- 无线旧画面的 codec 在接收器释放旧流时同步关闭；迟到的 UI 断开通知不会再关闭新流的 codec。

### DDI 与控制

检查内置镜像提取、固定摘要、缓存恢复、开发者模式、nonce/芯片选择、TSS、USB 上传、挂载、CoreDevice 隧道、HID 服务和快捷操作。

- 保留内置通用 DDI 与固定 SHA-256，不下载镜像；设备个性化签名仍使用 Apple HTTPS/TSS 和专用公开根证书。
- 开发者状态、服务创建、DDI 请求、上传写入、完成响应及关闭都有等待上限，取消后不会开始镜像上传。
- Apple TSS 使用最多两个独立、有 HTTPS 超时的签名线程。取消会话时不再等待 asyncio 默认线程池中的阻塞 HTTPS；已经发出的 HTTPS 请求可能在其守护线程内结束，但结果不能进入已取消的会话，也不会执行 USB 上传或挂载。
- 首次无线配对准备与 HID 初始化并行，避免无线信任/配对等待阻挡 USB 触控就绪。退出时取消并等待这项准备任务，再释放所属 RSD/tunnel。
- HID 服务及快捷按钮初始化被取消时，也关闭已经打开的连接。
- 模糊失败的触点按下可能已经到达手机。保留这类新触点槽位到退出清理快照，确保最终尝试发送抬起，不因本地尚未提交状态就遗漏释放。
- 隧道握手发送有时间边界，验证 RSD 端口和 IPv6 帧长度不超过协商 MTU。

### 无线发现、认证与关闭

检查双栈接收、Bonjour/NSD、PIN 跨 TCP 事务、签名认证、FairPlay、AES-CTR、无线控制选择、服务丢失和重试。

- 保留已补齐的跨 TCP PIN 和 binascii 导入。Pair-Verify 及传统 Pair-Setup 必须使用已完成 PIN 注册的发送端公钥，修复陌生公钥绕过配对记录的问题；验证失败清除本次认证状态。
- 取消无线解码任务时，等待已经开始的 Java/codec 回调结束，不把后台解码线程遗留给下一个流。
- NSD 索引由服务类型和名称共同组成，避免两种 RemotePairing 服务同名覆盖。
- 解析结果携带发现修订编号；服务丢失或重发现后忽略旧结果，防止已消失的设备重新进入候选列表。
- 应用关闭后迟到的注册/发现成功回调立即撤销对应 NSD 操作。
- 发现队列、有效服务数和 Python 候选集合有界；普通 RemotePairing 服务优先，服务更新立即触发重新选择，正在使用的服务丢失时取消旧控制任务。
- RemotePairing 设备标识符限制为安全的文件名字符，配对关闭等待有界，并在取消时清理连接。

### 安卓状态与重连

每次运行创建专属 `SessionCallbacks`，视频、状态、控制、配对及断开回调只属于创建它们的会话。UI 队列再次核对会话编号，迟到回调不再污染新连接。停止请求也携带会话编号，延迟执行的旧 stop 不能终止新会话。开始连接时重置解码诊断计时和尺寸请求状态；取消后拒绝重新启用正数触控能力。

## 本轮边界

本轮结束条件是：已修复本次静态审查能够确认的流程缺陷，并完成修改后的逐段复查及仅编译打包。没有执行任何本地或真机测试，不把旧版本的测试结果标成新版结果。

原 K60 日志中的 16 帧硬件积压、iPhone 的编码行为、真实 AirPlay 首帧与驱动实际是否应用低延迟参数，不能仅凭其他项目或静态代码证明完全消失。因此本轮不宣称所有设备已恢复满 60 fps 或不存在运行时缺陷。

## 交付

最终只执行 `:app:assembleDebug`，Gradle 构建成功。没有执行测试任务、lint、ADB 安装或设备运行。已有测试资料的时间戳和服务索引预期随接口修改作了同步调整，但未执行这些测试。

内置 DDI 包：`artifacts/iMirrorLite-DDI-v3-2026-10-14.apk`，61,350,552 字节；SHA-256：`bc664490c17a65a48244635b7e26fe1940e7d21e8a39028f4cc42c6055923a7d`。构建记录：`artifacts/build-static-review-v3-final-2026-10-14.log`。本轮可静态确认的修复已完成并收尾，不以未运行的测试或设备行为作为结论。
