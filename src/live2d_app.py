"""小念的 Live2D 桌面形象窗口（独立进程）。

用 pywebview 起一个透明、无边框、置顶的桌宠窗口，通过本地 HTTP 伺服
assets/live2d/ 下的页面与模型文件（.moc3 必须经 http 才能正确 fetch，
用 html 字符串加载会失败），页面渲染 Live2D 形象（或卡通占位）并支持文字气泡。
主进程（gui.py）通过本地 TCP 把小念的回复发过来，这里调用 JS 显示气泡。

一般不需手动运行——gui.py 在启动时会自动拉起本进程。
"""
import os
import sys
import json
import time
import socket
import shutil
import threading
import urllib.parse
import ctypes
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG

try:
    import webview
except Exception as e:  # pragma: no cover
    print("缺少 pywebview，无法启动形象窗口：", e)
    sys.exit(1)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))  # 项目根：HTTP 伺服以此为根，避免模型越界 404
ASSET_DIR = os.path.abspath(os.path.join(HERE, "..", "assets", "live2d"))
HTTP_PORT = int(CONFIG.get("live2d_http_port", 9743))
PORT = CONFIG["live2d_port"]
NAME = CONFIG["name"]
MODEL = CONFIG.get("live2d_model", "") or ""

window = None


def model_url():
    """把 .env 里的模型路径（相对项目根或绝对）转成本地 HTTP 地址。"""
    if not MODEL:
        return ""
    mpath = MODEL if os.path.isabs(MODEL) else os.path.abspath(os.path.join(ROOT, MODEL))
    try:
        rel = os.path.relpath(mpath, ROOT).replace(os.sep, "/")
    except Exception:
        rel = MODEL
    return "http://127.0.0.1:%d/%s" % (HTTP_PORT, rel)


# ---- 模型发现 / 切换 / 添加 ----
MODELS = []      # 可切换的模型列表（相对项目根的路径）
_idx = 0         # 当前模型索引

def discover_models():
    """扫描 assets/live2d 下所有 Cubism 模型（.model3.json / 同目录有 .moc 或 .moc3 的 .json）。"""
    found = []
    if os.path.isdir(ASSET_DIR):
        for root, _, files in os.walk(ASSET_DIR):
            for f in files:
                low = f.lower()
                if low.endswith(".model3.json"):
                    found.append(os.path.join(root, f))
                elif low.endswith(".json") and low != "model3.json":
                    base = os.path.splitext(f)[0]
                    if os.path.exists(os.path.join(root, base + ".moc")) or \
                       os.path.exists(os.path.join(root, base + ".moc3")):
                        found.append(os.path.join(root, f))
    if MODEL:
        ap = MODEL if os.path.isabs(MODEL) else os.path.abspath(os.path.join(ROOT, MODEL))
        if os.path.isfile(ap):
            found.insert(0, ap)
    rels = []
    for p in found:
        ap = p if os.path.isabs(p) else os.path.abspath(os.path.join(ROOT, p))
        rel = os.path.relpath(ap, ROOT).replace(os.sep, "/")
        if rel not in rels:
            rels.append(rel)
    return rels

def model_display(rel):
    name = os.path.basename(rel)
    for ext in (".model3.json", ".model.json", ".json"):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name

def detect_mouth_param(model_rel):
    """返回嘴型参数名：优先 .env 的 LIVE2D_MOUTH_PARAM；否则从 model3.json 的
    LipSync 组读取；都拿不到回退 ParamMouthOpenY。让换用嘴型参数名不同的模型时
    实时口型仍生效（如老模型用 ParamMouthOpen）。"""
    env_val = (CONFIG.get("live2d_mouth_param") or "").strip()
    if env_val:
        return env_val
    if not model_rel:
        return "ParamMouthOpenY"
    try:
        mpath = model_rel if os.path.isabs(model_rel) else os.path.abspath(os.path.join(ROOT, model_rel))
        if not os.path.isfile(mpath):
            return "ParamMouthOpenY"
        with open(mpath, encoding="utf-8") as f:
            data = json.load(f)
        groups = (data.get("FileReferences", {}).get("Groups")
                   or data.get("Groups") or [])
        for g in groups:
            if (g.get("Name") or "").lower() == "lipsync":
                ids = g.get("Ids") or []
                if ids:
                    return ids[0]
    except Exception:
        pass
    return "ParamMouthOpenY"


def build_page_url(model_rel):
    mhttp = "http://127.0.0.1:%d/%s" % (HTTP_PORT, model_rel) if model_rel else ""
    mouth = detect_mouth_param(model_rel)
    # 末尾加时间戳作为缓存破坏：WebView2 有时会缓存顶层文档(index.html)，
    # 导致对 index.html 的修改不生效（水印/⚙ 失灵）。带不同 query 即强制重新拉取。
    return "http://127.0.0.1:%d/assets/live2d/index.html?model=%s&mouth=%s&_=%d" % (
        HTTP_PORT, urllib.parse.quote(mhttp), urllib.parse.quote(mouth), int(time.time() * 1000))

def switch_model(model_rel, toast=True):
    if window is None or not model_rel:
        return
    url = build_page_url(model_rel)
    if toast:
        url += "&toast=" + urllib.parse.quote(model_display(model_rel))
    try:
        window.load_url(url)
    except Exception as e:
        print("切换模型失败：", e)

def save_default_model(rel):
    try:
        p = os.path.join(ROOT, ".env")
        if not os.path.isfile(p):
            return
        lines = open(p, encoding="utf-8").read().splitlines()
        out, replaced = [], False
        for ln in lines:
            if ln.startswith("LIVE2D_MODEL="):
                out.append("LIVE2D_MODEL=" + rel)
                replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append("LIVE2D_MODEL=" + rel)
        open(p, "w", encoding="utf-8").write("\n".join(out) + "\n")
    except Exception as e:
        print("写回默认模型失败：", e)

def add_model_via_dialog():
    global _idx
    if window is None:
        return
    try:
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=(("Live2D 模型 (*.model3.json;*.json)", "*.*"),),
        )
    except Exception as e:
        print("打开文件对话框失败：", e)
        return
    if not result:
        return
    src_json = result[0]
    src_dir = os.path.dirname(src_json)
    base = os.path.basename(src_dir) or "model"
    dest_dir = os.path.join(ASSET_DIR, "models", base)
    try:
        if os.path.abspath(dest_dir) != os.path.abspath(src_dir):
            if os.path.isdir(dest_dir):
                shutil.rmtree(dest_dir)
            shutil.copytree(src_dir, dest_dir)
    except Exception as e:
        print("复制模型失败：", e)
        return
    new_rel = None
    for f in os.listdir(dest_dir):
        if f.lower().endswith(".model3.json") or f.lower() == "model.json":
            new_rel = os.path.relpath(os.path.join(dest_dir, f), ROOT).replace(os.sep, "/")
            break
    if new_rel is None:
        print("未在原模型中找到 .model3.json / model.json")
        return
    MODELS.append(new_rel)
    _idx = len(MODELS) - 1
    save_default_model(new_rel)
    switch_model(new_rel)

def hotkey_loop():
    global _idx
    user32 = ctypes.windll.user32
    MOD_CTRL, MOD_ALT = 0x0002, 0x0001
    # 关键：hWnd 必须传 NULL(0)，不能传 HWND_MESSAGE(-3)，
    # 否则本机 RegisterHotKey 返回 1400(INVALID_WINDOW_HANDLE) 注册失败、热键失灵。
    HWND_NULL = 0
    VK_RIGHT, VK_LEFT, VK_O = 0x27, 0x25, 0x4F
    WM_HOTKEY = 0x0312

    def reg(i, vk):
        if not user32.RegisterHotKey(HWND_NULL, i, MOD_CTRL | MOD_ALT, vk):
            print("注册热键失败(可能被占用)：", i)

    reg(1, VK_RIGHT)   # Ctrl+Alt+→ 下一个模型
    reg(2, VK_LEFT)    # Ctrl+Alt+← 上一个模型
    reg(3, VK_O)       # Ctrl+Alt+O 添加模型

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            i = msg.wParam
            if i == 1:
                _idx = (_idx + 1) % len(MODELS)
                switch_model(MODELS[_idx])
            elif i == 2:
                _idx = (_idx - 1) % len(MODELS)
                switch_model(MODELS[_idx])
            elif i == 3:
                add_model_via_dialog()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def start_http_server():
    """伺服“项目根目录”，使 index.html 与模型资源（.moc3/.model3.json/贴图）都可被 fetch。
    以项目根为根目录，避免模型放在 assets 目录外时出现 404。"""
    os.makedirs(ROOT, exist_ok=True)
    handler = SimpleHTTPRequestHandler
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler)
    def _make_no_cache(cls):
        _orig_end = cls.end_headers
        def _end_headers(self):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            _orig_end(self)
        cls.end_headers = _end_headers
        return cls
    H = type("H", (handler,), {"directory": ROOT, "log_message": lambda self, *a, **k: None})
    httpd.RequestHandlerClass = _make_no_cache(H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def serve():
    """本地 TCP 服务：接收主进程发来的 {"text":..., "name":...} 并显示气泡。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(8)
    while True:
        try:
            conn, _ = srv.accept()
            data = conn.recv(65536).decode("utf-8", "ignore")
            conn.close()
            if not data:
                continue
            msg = json.loads(data)
            # 实时口型：高频(约 15~20Hz)把音频能量传过来，直接驱动嘴张合
            if "mouth" in msg:
                if window is not None:
                    try:
                        window.evaluate_js(
                            "window.setMouth && window.setMouth(%s)" % json.dumps(float(msg["mouth"]))
                        )
                    except Exception:
                        pass
                continue
            # 停止说话：口型归零 + 淡出气泡（音频播完时由主进程发送）
            if msg.get("talk_stop"):
                if window is not None:
                    try:
                        window.evaluate_js("window.stopTalk && window.stopTalk()")
                    except Exception:
                        pass
                continue
            # 切换 Live2D 模型（由主进程设置面板的「Live2D 形象」下拉触发）
            if "switch_live2d_model" in msg:
                rel = msg["switch_live2d_model"]
                if rel:
                    try:
                        global _idx
                        if rel in MODELS:
                            _idx = MODELS.index(rel)
                        switch_model(rel)            # 重新加载页面加载新模型
                        save_default_model(rel)       # 写回 .env，下次启动默认就是这个
                    except Exception as e:
                        print("切换 Live2D 模型失败：", e)
                continue
            # 模型大小（由主进程控制台滑块驱动）
            if "scale" in msg:
                if window is not None:
                    try:
                        window.evaluate_js(
                            "window.__setModelScale && window.__setModelScale(%s)"
                            % json.dumps(float(msg["scale"]))
                        )
                    except Exception:
                        pass
                continue
            # 气泡显隐（由主进程控制台复选框驱动）
            if "bubble" in msg:
                if window is not None:
                    try:
                        window.evaluate_js(
                            "window.__setBubble && window.__setBubble(%s)"
                            % json.dumps(bool(msg["bubble"]))
                        )
                    except Exception:
                        pass
                continue
            # 复位模型大小/位置（由主进程控制台按钮驱动）
            if msg.get("reset"):
                if window is not None:
                    try:
                        window.evaluate_js("window.__resetModel && window.__resetModel()")
                    except Exception:
                        pass
                continue
            # 兼容旧的独立动作消息
            if msg.get("action") == "motion":
                if window is not None:
                    window.evaluate_js(
                        "window.playMotion && window.playMotion(%s)" % json.dumps(msg.get("args", []))
                    )
                continue
            # 跳跃 / 转身（前端代码模拟动作，不依赖模型动作文件）
            if msg.get("action") == "jump":
                if window is not None:
                    window.evaluate_js("window.playJump && window.playJump()")
                continue
            if msg.get("action") == "turn":
                if window is not None:
                    window.evaluate_js("window.playTurn && window.playTurn(-1)")
                continue
            text = msg.get("text", "")
            name = msg.get("name", NAME)
            if window is not None and text:
                window.evaluate_js(
                    "window.showBubble(%s)" % json.dumps({"name": name, "text": text})
                )
            # 同一条消息若带 motion 标记，说话时同时触发反馈动作
            if window is not None and msg.get("motion"):
                window.evaluate_js(
                    "window.playMotion && window.playMotion(%s)" % json.dumps(msg.get("args", []))
                )
            # 开始说话：锁定当前气泡为“说话气泡”，进入实时口型模式
            if window is not None and msg.get("talk_start"):
                window.evaluate_js("window.startTalk && window.startTalk()")
        except Exception:
            pass


class Api:
    def close_window(self):
        """由页面上的关闭按钮调用，关闭形象窗口（不影响聊天窗口）。"""
        if window is not None:
            window.destroy()

    def play_motion(self, group="tap", index=-1):
        """由 Python 触发模型动作（说话/点击等），仅对真 Live2D 生效。"""
        if window is not None:
            window.evaluate_js(
                "window.playMotion && window.playMotion(%s)" % json.dumps([group, index])
            )

    def toggle_input(self):
        """由页面上的「输入框」按钮调用，反向通知 gui 进程切换输入条显隐。"""
        import socket as _sock
        import json as _json
        port = int(CONFIG.get("gui_control_port", 9744))
        try:
            with _sock.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.sendall(_json.dumps({"toggle_input": True}).encode("utf-8"))
        except Exception as e:
            print("toggle_input 通知失败：", e)


def on_ready():
    # 窗口就绪后，移动到屏幕右下角
    try:
        screens = webview.screens
        if screens:
            sw, sh = screens[0].width, screens[0].height
            window.move(max(0, sw - 440), max(0, sh - 660))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 透明窗口补丁：pywebview 只把 WebView2 控件的背景设成了透明
# （DefaultBackgroundColor=Transparent），但底层 WinForms 表单仍是默认灰白底色，
# 会透过透明控件露出来形成“白板”。这里用 TransparencyKey 方案把表单也变成真正
# 的透明窗口（系统自动分层并抠掉指定颜色），让桌面透出来，消除白板。
# ----------------------------------------------------------------------------
try:
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Drawing import Color

    import webview.platforms.winforms as _wf

    _BrowserForm = _wf.BrowserView.BrowserForm
    _orig_init = _BrowserForm.__init__

    def _transparent_form_init(self, window, cache_dir):
        _orig_init(self, window, cache_dir)
        if not getattr(window, "transparent", False):
            return
        try:
            # 用 TransparencyKey 方案（最稳）：把表单背景设为一种页面不会用到的稀有色，
            # 并把它作为透明键。系统会把该色像素抠掉、透出桌面；WebView2 的透明页面区域
            # 会露出表单背景（该稀有色），从而一起透出桌面 —— 消除“白板”。
            self.BackColor = Color.Magenta
            self.TransparencyKey = Color.Magenta
        except Exception as _e:
            # 任何一步失败都不要影响窗口创建（最坏只是仍白，但不会崩）
            print("透明补丁单步失败：", _e)

    _BrowserForm.__init__ = _transparent_form_init
except Exception as _patch_err:  # pragma: no cover
    print("透明窗口补丁未应用（白板可能仍在）：", _patch_err)


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    start_http_server()

    # 发现所有可用模型，并定位 .env 指定的当前模型
    MODELS = discover_models()
    if not MODELS:
        MODELS = [MODEL] if MODEL else [""]
    _idx = 0
    if MODEL:
        cur = MODEL if os.path.isabs(MODEL) else os.path.relpath(
            os.path.abspath(os.path.join(ROOT, MODEL)), ROOT).replace(os.sep, "/")
        if cur in MODELS:
            _idx = MODELS.index(cur)

    # 全局热键（零依赖，ctypes 实现）：
    #   Ctrl+Alt+→  下一个模型      Ctrl+Alt+←  上一个模型      Ctrl+Alt+O  添加模型
    threading.Thread(target=hotkey_loop, daemon=True).start()

    page_url = build_page_url(MODELS[_idx])
    window = webview.create_window(
        NAME,
        url=page_url,
        transparent=True,
        frameless=True,
        on_top=True,
        width=400,
        height=620,
        js_api=Api(),
    )
    webview.start(on_ready)
