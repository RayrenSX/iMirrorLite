# 主页与全屏视觉调整

APK SHA-256：`fe267d7252ac612f7add5452380e2e940a2bf3d19e1cc21994eb74eddd6d9d29`。

- 主页使用浅灰白背景与青绿强调色，品牌标志、连接按钮、状态卡和快捷操作栏使用统一圆角与线条图标。去掉系统默认按钮阴影，增加按下反馈。
- 未连接与会话结束时显示设备图标和简短连接引导；图标由 Drawable 绘制，不依赖字体符号或位图缩放。
- 全屏使用纯黑背景，三键仍与镜像同宽、紧贴镜像底部；退出入口改为右下角的收起图标，避开镜像顶部状态栏。安卓返回键仍可退出全屏。
- 自定义三键使用所绑定动作的图标与名称，长按配置和保存逻辑沿用原实现。未配置左键保持原来的无操作行为。
- 横屏快捷操作放在右侧两列布局，竖屏为底部六列。横竖屏与全屏复用已有 SurfaceView，视频比例与触控坐标规则不变。

验证：assembleDebug 与 lintDebug 通过（0 errors、16 warnings，主要是已有 API / 国际化提示）；最终 APK 已安装到 EBG-AN00。已检查真实投屏下的普通 / 全屏、横竖屏、右下角退出入口及安卓返回退出。旋转检查后恢复原来的自动旋转设置。最终版本投屏收到约 60 fps、显示约 58 fps，为瞬时诊断采样，非长期性能验收。

更新后的首次重连遇到 QuickTime 握手超时，释放后重试恢复视频。后续 RemoteXPC 控制握手两次超时，最终仍为视频正常、控制通道未连接；需要重新插拔数据线并保持 iPhone 解锁后复测。没有修改 USB、RemoteXPC、视频接收或解码实现，也没有把此前控制成功当成最终版本控制验收。

截图在忽略目录 `artifacts`：ui-refresh-home-empty.png、ui-refresh-home-live.png、ui-refresh-full-portrait.png、ui-refresh-home-landscape.png、ui-refresh-full-landscape.png。构建记录为 build-ui-refresh.log。
