# 无线实测与控制发现修复 · 2026-10-14

设备：EBG-AN00 / Android 12 / ARM64，与 iPhone18,3 / iOS 26.6.2。两台设备使用同一 Wi-Fi。用户提供了短时 iPhone 测试，随后要求停止 iPhone 测试，继续本地修复并允许安卓测试。

## 已确认的实测结果

- iPhone 的 AirPlay PIN、Pair-Verify、FairPlay、SETUP / RECORD 握手成功。
- 安卓实际显示 498×1080 H.264 无线画面，07:38:53 收到首个编码帧，07:38:53.584 显示到 SurfaceView。用户确认「好了」「可以用了又试了一遍」。
- 无线画面连续运行时，日志中有约 60 fps 接收、59–60 fps 显示的窗口，也有较低帧率窗口；没有验收长时间稳定性或始终满帧。
- USB 主从角色最初反向；切换安卓为数据主机后，安卓识别 iPhone18,3、完成 DDI 检查、建立五指 HID 与快捷操作通道。用户操作期间有触控发送、主页和多任务成功记录。
- iPhone 确认信任后，07:43:02 安卓保存了独立无线控制配对记录。拔线后无线视频仍正常，用户确认无线控制不能使用。

## 本地修复

AirPlay 实际连接使用 iPhone 的 IPv6 链路本地地址，控制服务的 DNS-SD 广播同时发布 IPv4 与 IPv6。旧代码只比较 Android NSD 返回的单个地址与投屏发送端地址，可能把同一 iPhone 当成不同设备。

现在保留 AirPlay IPv6 接口范围，补充解析同一个 DNS-SD SRV 主机的 A / AAAA 记录。只有记录内包含当前投屏发送端地址的控制服务才成为候选；优先使用该主机发布的 IPv4 地址连接。不会仅因为两台设备处于同一网段就合并身份。

补充发现使用临时 UDP 端口和单播应答查询，不占用 Android 系统 mDNS 服务的 5353 端口。发送端变化、服务丢失和过时异步结果会清理地址关联；关闭会话会取消发现任务并等待套接字关闭。已有无线配对时，界面显示正在查找服务，不再提示用户重复首次 USB 配对。

视频按 AirPlay 的四字节 NAL 长度拆包，接入上游描述的已配对与旧版流密钥格式探测，并记录配置和密钥格式诊断。首轮仍观察到一次无效初始视频包，重试后成功；不能据此宣称频繁连接的全部异常已解决。

## 修复后验证

- 本地 83 项回归通过，包含混合地址发现、同网段其他设备拒绝、服务撤销、IPv6 接口范围保留、迟到结果隔离、发送端更换和真实 UDP 查询。
- 开发 APK、安卓测试 APK 和 lint 构建成功；lint 为 0 错误、21 警告。
- EBG-AN00 当前 APK 已安装，安卓运行时检查返回 `RUNTIME_SMOKE_OK`。合成 DNS-SD 服务把 IPv6 投屏身份关联到同一主机的 IPv4，随后通过真实 TLS-PSK 套接字和用户态 IPv6 隧道；同时覆盖 PIN、原生 PlayFair、加密视频硬解、NSD 广播、PixelCopy 和五指 MotionEvent。
- 合成测试的 AirPlay 身份与配对文件使用临时目录，不修改日常使用的信任记录。真实无线控制配对记录仍保留在应用私有目录。

修复后的真实 iPhone 无线控制仍待复测；安卓合成链路通过不能代替手机实际接收触控的验证。

证据：`artifacts/wireless-live-2026-10-14.log`、`artifacts/wireless-live-previous-2026-10-14.log`、`artifacts/wireless-local-repair-tests.log`、`artifacts/wireless-local-repair-build.log`、`artifacts/wireless-local-repair-runtime-arm64.log`。
