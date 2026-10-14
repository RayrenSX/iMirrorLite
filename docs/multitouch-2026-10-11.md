# iPhoneMirror 五指触控移植与真机验收

设备：EBG-AN00 / Android 12 API 31 ARM64，USB 直连 iPhone 12 mini / iPhone13,1 / iOS 18.7.8。无线 ADB 和电脑热点仅用于开发安装及收集 Safari 事件，触控报告由安卓直接经 USB 发送给 iPhone。

## 移植内容

来源为桌面 iPhoneMirror 的 `tools/usb_touch_bridge.py`、`docs/FIVE_POINT_TOUCH.md` 和 `tests/five_point_touch_test.py`。源文件摘要记录在 `third_party/provenance.json`。

- 已知 CoreDevice touchscreen 服务 257 使用 58 字节五触点报告，即使设备未发布 ReportDescriptor 也可启用。可解析的并行描述符仍优先使用，未知触控面保留单指回退。
- 按下/移动标志为 `0xC0 | slot`，抬起为 `slot`；槽位 0–4、身份表偏移 40–44 按槽位索引、48 位单调时钟时间戳偏移 45–50，其余保留区清零。
- Android pointerId 对应稳定槽位。每批发送完整快照，静止手指每秒保活；第六指的 DOWN/MOVE 不会挤占或复活已有触点。
- 先验证整个输入批次，在传输成功后提交触点状态。满容量批次同时抬起和按下时先送释放，再复用槽位；发送失败或取消保留状态供最终清理。

## 验证结果

51 项 Python 回归通过。新增九项验证格式字段、独立解码、五指快照、未知设备回退、空槽复用、取消和发送失败后的状态。Android ARM64 打包运行时返回 `RUNTIME_SMOKE_OK`，包含实际 MotionEvent 的五指路由、指序变化、第六指拒绝和取消，同时保留原有 Python / 密码库 / USB JNI / H.264 解码像素检查。

在 iPhone Safari 打开开发测试页，由开发专用 `scripts/TouchInjector.java` 注入安卓原生 MotionEvent，走正式 MainActivity → TouchRouter → Python → USB → iPhone 通道。独立证据为 Safari 的 TouchEvent，保存于忽略目录 `artifacts/android-five-point-live/injected-events.json`。2026-10-11 23:26 左右的九个事件结果如下：

| 操作 | iPhone 实际事件与结果 |
| --- | --- |
| 逐一按下五指 | touchstart 1、2、3、4、5；五个不同 ID 944419340–344 |
| 静止保持后移动第三指 | 五指静止约 1.7 秒，无提前抬起；只有 944419342 的 Y 从 315 变为 437，其余四指 ID 和坐标不变 |
| 第六指按下 | 没有新 touchstart，保持原五指 |
| 第三指独立抬起 | touchend 仅包含 944419342，留下原四指 |
| 移动此前拒绝的第六指 | 没有复活触点，仍为四指 |
| 第六指抬起后重新按下 | 新 touchstart 944419345，复用空槽回到五指，原四指保持不变 |
| Android ACTION_CANCEL | iPhone touchend 同时释放剩余五指，touches 归零 |

用户随后实际操作双指缩放、旋转或五指手势，回复“正常，无跳点或残留”。自动注入验证了传输和事件独立性，用户检查补充了实体安卓触摸屏的实际体验。此次验收不代表其他 iPhone / Android 组合均已验证。

开发观察服务器和 shell 注入器均不属于 APK。只在测试页前台时发送验收手势，未对用户内容执行多指输入。视频接收、解码及短排程算法未因多指移植而修改。
