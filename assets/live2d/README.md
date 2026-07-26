# 小念的 Live2D 桌面形象

小念现在有一个浮在桌面上的形象窗口：透明、无边框、置顶，会显示她的回复文字气泡。
默认是纯 CSS 卡通占位（零依赖、立即可见）；配置模型后自动切换为真正的 Live2D 模型。

## 零依赖已可用（卡通占位 + 文字气泡）

在 `.env` 里保持：

```
LIVE2D_ENABLED=true
LIVE2D_MODEL=
```

启动 `python src/main.py` 后，桌面右下角会出现小念的形象窗口，她说的话会浮成气泡。
（窗口右上角 `×` 可关闭形象窗口，不影响左侧聊天窗口。）

## 启用真正的 Live2D 模型

Live2D 需要浏览器引擎渲染。本项目用 `pywebview`（Windows 上的 Edge WebView2）：

1. **安装 WebView2 运行时**（系统组件，约 100MB，装到 C 盘系统目录）：
   运行项目根目录下的 `webview2_installer.exe`（已为你下载好），按提示安装即可。
   或在微软官网下载 "WebView2 Runtime" 独立安装包。
2. **准备一个 Live2D 模型**。推荐 **Cubism 2 格式**的免费模型（live2d-widget 支持最稳），
   例如社区常用的：
   - `live2d-widget-model-shizuku`（涩喵）
   - `live2d-widget-model-hijiki`（ Hijiki）
   - `live2d-widget-model-tororo`（Tororo）
   
   从 GitHub 下载对应仓库，把里面的模型目录（含 `model.json`、贴图、物理/动作文件）
   放到：`assets/live2d/models/<模型名>/`
3. 在 `.env` 里填写模型路径并开启：
   ```
   LIVE2D_ENABLED=true
   LIVE2D_MODEL=assets/live2d/models/shizuku/model.json
   ```
4. 重新启动 `python src/main.py`。

> 说明：live2d-widget 在首次加载时会从 CDN 拉取渲染库（需联网）。
> 若要完全离线，可把 `live2d-widget.min.js` 下载到 `assets/live2d/sdk/` 并改 `app.js` 的引用。
> Cubism 3/4 模型（`model3.json`）也能被 live2d-widget 加载，但兼容性略低于 Cubism 2，
> 建议优先用 Cubism 2 模型获得最佳效果。

## 自定义

- 气泡样式、卡通形象、模型调整面板：都写在 `index.html` 的内联 `<style>` 与脚本里
  （`#avatar` / `.bubble*` / `#model-panel` / `#mp-toggle`）。
- 模型加载窗口大小/位置：改 `src/live2d_app.py` 里 `create_window` 的 `width/height` 与 `on_ready` 的右下角偏移。
- **运行时调整模型**：形象窗口右下角 `⚙` 可开面板调「大小」；在形象上**按住拖动**可移动位置，
  轻点切表情。大小与位置按模型记忆在 `localStorage`，下次启动自动恢复。
