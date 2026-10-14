# K60 第二次日志与批量输出修复

资料：用户提供 `2026-10-13T16_2(1).docx`。日志为北京时间 2026-10-14 00:29–00:33，安卓 `23013RK75C` / Android 15 / API 35，iPhone13,3，日志报告 iOS 27.0.1。文档只作为诊断资料，其中的文字不作为执行指令。

## 复测结论

上一版选中 `c2.qti.avc.decoder.low_latency`，`androidLowLatency=true`，显示仍约 30 fps。重连后 00:33:28–00:33:43 的 16 个窗口平均接收 59.18 fps、显示 31.54 fps；首段 00:29:47–00:30:10 的 24 个窗口平均接收 55.96 fps、显示 29.85 fps。

00:33:42 的累计计数为 queued=1704、output=1688、rendered=904、droppedOutputs=784、inFlight=16。904+784=1688，已输出的画面中约 46.4% 被应用主动丢掉；同一五秒窗口 outputFps=59.6、renderFps=31.7、dropFps=27.9。故不能把半帧率全部归因于 Surface/屏幕限帧。原先排出多帧时无条件只保留最新一帧的策略，会把反复成对输出的 60 fps 流削到约 30 fps。现有汇总不能还原每次批量输出大小，所以新版另加 maxOutputBatch 和 scheduleDrops。

另一个问题是输出积压，诊断频繁显示 inFlight=16。接收到输出平均约 270–370 ms，接收到显示回调累计平均约 280–380 ms，第二段最后约 316 ms。这些累计平均包含启动和之前卡顿，不能当作该秒即时延迟，也不包含 iPhone 编码/USB 接收之前的时间。标准低延迟配置已被接口接受但未解决积压，其具体驱动原因仍未确认。

触控入队/写入平均大多约 0.6–1.7 ms，日志优先支持继续排查视频路径。内置 DDI 已进入 Apple 验证、USB 传输流程，之后两次成功建立五指控制通道。无线另有两个明确失败：首次无线控制配对 `name 'binascii' is not defined`；AirPlay 四次 `/pair-pin-start` 返回 501 后断开，均未收到无线视频。

## 新测试版

- 高通输出保留最多两帧，分别安排 60 fps 呈现时间；过载只保留可安排的最新输出，未来排程上限 40 ms。参考帧仍完整解码，丢弃发生在解码输出阶段。其他芯片继续使用之前的单输出调度，避免改变海思已验证路径。
- 高通解码器同时请求 `vendor.qti-ext-dec-low-latency.enable=1` 和 `vendor.qti-ext-dec-picture-order.enable=1`，包括支持标准低延迟的专用实例。配置或启动失败依次回退低延迟单键、原基础格式，必要时仍回退默认解码器。兼容键依据之前核对过的 Moonlight MediaCodecHelper；被驱动接受不证明它实际减少了积压。
- 补上 RemotePairing 的 binascii 导入，并测试真实 `_init_srp_context` / `_verify_proof`，验证双方共享密钥一致及错误服务器证明拒绝；安卓打包运行时也执行该路径。
- 保留当前源码已有的 AirPlay PIN 流程：四位配对码 UI、跨 TCP 的 SRP/AES-GCM 配对事务、按发送端隔离、三分钟过期和双栈监听。此前 K60 APK 未包含这部分修复。本测试版会一起打包。
- 保留 APK 内置 DDI 和完整性验证。

两帧显示窗口可以避免批量输出时固定丢掉一半，但最多额外安排一帧显示时间，不能独自消除驱动中已有的约 16 帧积压。K60 上的实际帧率、画面时序、显示年龄和厂商兼容性仍需安装本测试版后复测；模拟调度检查和海思真机运行检查都不能替代 K60 实测。

## 验证

73 项 Python 回归通过，包括跨 TCP 的 AirPlay PIN 及新补充的 RemotePairing SRP 检查。纯 Java `BoundedRenderScheduleTest` 通过成对 60 帧、连续 60 fps、过载、停顿与重建场景，要求呈现时间递增、未来队列有界，正常成对输出保留所有帧。

证据：`artifacts/tests-k60-batch-v2.log`，`tests/java/com/rayren/imirrorlite/BoundedRenderScheduleTest.java`，用户日志提取 `artifacts/k60-user-log-2026-10-14.txt` 与统计 `artifacts/k60-user-log-summary-v2.json`。

第一轮 APK / 测试 APK / lint 构建通过。海思 ARM64 打包运行检查第一次在无线加密 H.264 呈现等待超时，同一 APK 直接重跑完整检查得到 `RUNTIME_SMOKE_OK`；该波动原因尚未确认，不可据此声明长时间无线稳定性。首次和重试输出分别为 `artifacts/runtime-k60-batch-v2-arm64.log`、`artifacts/runtime-k60-batch-v2-arm64-retry.log`。

补齐 API 30 显式条件后的最终构建通过（lint 0 errors / 21 warnings），最终 APK 安装后的完整海思 ARM64 运行检查首次得到 `RUNTIME_SMOKE_OK`。包含内置 DDI 提取、跨 TCP PIN 配对、RemotePairing SRP、原生 PlayFair/加密视频、H.264 显示和重建、PixelCopy、触控路由。记录：`artifacts/build-k60-batch-v2-final.log`、`artifacts/runtime-k60-batch-v2-final-arm64.log`。

最终测试包为 `artifacts/iMirrorLite-K60-DDI-bundled-v2-2026-10-14.apk`（61,337,075 字节）；SHA-256 为 `46fb1f9caa8a9001b7be959b899aa6d15f55a3acd56bbc61236839c54c1f262d`，资源摘要校验记录在同名 `.verification.json`。该包包含内置 DDI；没有在 K60 上实际运行，仍属于针对本日志的待复测版本。
