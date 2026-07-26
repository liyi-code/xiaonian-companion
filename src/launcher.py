"""AppLauncher：负责“找到应用”并“真正启动它”。

设计要点：
1. 与对话逻辑、GUI 完全解耦，可独立测试。
2. 启动 .lnk 快捷方式时，先解析出真实 exe，用 subprocess.Popen 直接从当前
   （交互）进程 CreateProcess 拉起 —— 这是 Windows 上最可靠的方式，绕过会
   “静默失败”的 ShellExecute / os.startfile / explorer。
3. 启动后轮询校验进程是否真的起来（psutil / tasklist），不再“假成功”。
4. 每一步都写入 launch.log，便于定位“说打开却没反应”的真正原因。
"""

import os
import re
import time
import subprocess

try:
    import winreg
except Exception:
    winreg = None

try:
    import psutil
except Exception:
    psutil = None


_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launch.log"
)


def _log(msg):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 查找
# --------------------------------------------------------------------------- #
def _get_known_folder_path(csidl):
    """通过 Windows API 获取真正（含中文系统本地化）的特殊文件夹路径。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        res = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        if res == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


# 这些关键词的快捷方式通常是卸载/帮助/说明，不能当“打开软件”来启动
_BAD_SHORTCUT_KEYWORDS = (
    "卸载", "uninstall", "修复", "repair", "帮助", "help", "readme",
    "说明", "setup", "安装", "install", "官网", "反馈", "升级", "配置",
)


def _find_shortcut(query):
    """在开始菜单和桌面的快捷方式中按名称查找 .lnk。
    返回 (lnk路径 或 None, 所有快捷方式名称列表)。桌面优先、排除卸载类、打分排序。"""
    query = query.lower().replace(".lnk", "").replace(" ", "").strip()
    roots = []
    desk = _get_known_folder_path(0x0010)     # 桌面
    if desk:
        roots.append((desk, 0))
    cdesk = _get_known_folder_path(0x0019)    # 公共桌面
    if cdesk:
        roots.append((cdesk, 0))
    sm = _get_known_folder_path(0x000B)       # 开始菜单
    if sm:
        roots.append((os.path.join(sm, "Programs"), 1))
    csm = _get_known_folder_path(0x0016)      # 公共开始菜单
    if csm:
        roots.append((os.path.join(csm, "Programs"), 1))
    # 兜底：直接拼中英文路径
    up = os.path.expanduser("~")
    roots.append((os.path.join(up, "Desktop"), 0))
    roots.append((os.path.join(up, "桌面"), 0))
    roots.append((r"C:\Users\Public\Desktop", 0))

    candidates = []
    labels = []
    for root, prio in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                raw = f[:-4]
                norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", raw.lower())
                norm = norm.replace("快捷方式", "")
                labels.append(raw)
                if not (query in norm or norm in query):
                    continue
                if any(k in raw.lower() for k in _BAD_SHORTCUT_KEYWORDS):
                    score = -100
                elif norm == query:
                    score = 100
                elif norm.startswith(query) or query.startswith(norm):
                    score = 80
                else:
                    score = 50
                score -= prio * 10
                candidates.append((score, os.path.join(dirpath, f)))

    if not candidates:
        return None, labels
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1], labels


def _find_app_path(query):
    """在注册表 App Paths 中按名称查找可执行文件路径。"""
    if winreg is None:
        return None
    query = query.lower().replace(".exe", "")
    for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
            for i in range(winreg.QueryInfoKey(key)[0]):
                sub = winreg.EnumKey(key, i)
                if query in sub.lower() or sub.lower().replace(".exe", "") in query:
                    with winreg.OpenKey(key, sub) as sk:
                        path = winreg.QueryValue(sk, None)
                        if path and os.path.exists(path.strip('"')):
                            return path.strip('"')
        except Exception:
            pass
    return None


def _resolve_lnk_target(lnk):
    """读取 .lnk 指向的真实目标路径（只读，不启动）。失败返回 None。"""
    try:
        safe = lnk.replace("'", "''")
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{safe}');"
              "$s.TargetPath")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=10)
        t = (out.stdout or "").strip()
        return t if t and os.path.exists(t) else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _process_running(exe_basename):
    """判断某 exe 是否正在运行（兼容单实例应用）。"""
    exe_basename = exe_basename.lower()
    if psutil is not None:
        try:
            for p in psutil.process_iter(["name"]):
                if (p.info.get("name") or "").lower() == exe_basename:
                    return True
        except Exception:
            pass
    try:
        out = subprocess.run("tasklist", capture_output=True, text=True, timeout=10)
        return exe_basename in out.stdout.lower()
    except Exception:
        return False


def _popen_and_verify(target, cwd):
    """直接 Popen 拉起 exe，并轮询验证进程是否真的起来。返回 (ok, msg)。"""
    try:
        p = subprocess.Popen([target], cwd=cwd)
    except Exception as e:
        return False, f"Popen 失败: {e}"
    name = os.path.basename(target).lower()
    for _ in range(10):          # 最多等待约 5 秒
        time.sleep(0.5)
        if _process_running(name):
            return True, ""
    if p.poll() is None:         # 我们自己拉起的进程还活着，也算启动成功
        return True, ""
    return False, f"启动后未检测到进程（{name}）"


# --------------------------------------------------------------------------- #
# 启动器
# --------------------------------------------------------------------------- #
class AppLauncher:
    def __init__(self):
        self._run_on_main = None

    def bind_main_thread(self, fn):
        """GUI 传入一个“在主线程执行 fn”的函数（如 root.after），用于需要
        UI 线程上下文的 os.startfile。"""
        self._run_on_main = fn

    def _launch_path(self, path):
        """启动一个具体路径（.lnk / .exe / 文件 / 网址）。返回 (是否成功, 说明)。"""
        _log(f"启动: {path}")
        low = path.lower()

        # 方法1：.lnk 解析出真实 exe 后用 Popen 直接拉起（最可靠，可验证）
        if low.endswith(".lnk"):
            r = _resolve_lnk_target(path)
            if r:
                _log(f"  解析到目标: {r}")
                ok, err = _popen_and_verify(r, cwd=os.path.dirname(r))
                if ok:
                    _log(f"  [1] lnk>Popen 成功: {r}")
                    return True, f"已启动：{r}"
                _log(f"  [1] lnk>Popen 失败: {err}")
            else:
                _log("  无法解析 lnk 目标（PowerShell/WScript 可能不可用）")

        # 方法2：直接是可执行文件 —— Popen 直接拉起
        if low.endswith(".exe"):
            ok, err = _popen_and_verify(path, cwd=os.path.dirname(path))
            if ok:
                _log(f"  [2] exe>Popen 成功: {path}")
                return True, f"已启动：{path}"
            _log(f"  [2] exe>Popen 失败: {err}")

        # 方法3：cmd start（与双击快捷方式等价，不依赖解析，失败会抛异常）
        try:
            subprocess.Popen(
                f'cmd /c start "" "{path}"',
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            _log(f"  [3] cmd/start 已调用: {path}")
            return True, f"已启动：{path}"
        except Exception as e:
            _log(f"  [3] cmd/start 失败: {e}")

        # 方法4：主线程 os.startfile（对本地文件最原生，需要 UI 线程上下文）
        if self._run_on_main is not None:
            import threading as _th
            ev = _th.Event()
            res = {}
            def _run():
                try:
                    os.startfile(path)
                    res["ok"] = True
                except Exception as e:
                    res["ok"] = False
                    res["err"] = str(e)
                ev.set()
            try:
                self._run_on_main(_run)
                if ev.wait(timeout=15) and res.get("ok"):
                    _log(f"  [4] os.startfile 成功: {path}")
                    return True, f"已启动：{path}"
                else:
                    _log(f"  [4] os.startfile 失败: {res.get('err', '主线程未执行')}")
            except Exception as e:
                _log(f"  [4] os.startfile 调度失败: {e}")
        else:
            try:
                os.startfile(path)
                _log(f"  [4b] os.startfile(后台) 成功: {path}")
                return True, f"已启动：{path}"
            except Exception as e:
                _log(f"  [4b] os.startfile(后台) 失败: {e}")

        # 方法5：explorer
        try:
            explorer = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe"
            )
            subprocess.Popen([explorer, path])
            _log(f"  [5] explorer 已调用: {path}")
            return True, f"已启动：{path}"
        except Exception as e:
            _log(f"  [5] explorer 失败: {e}")

        return False, "所有启动方式均失败（详见 launch.log）"

    def open(self, name):
        """按名称/路径打开应用。返回 (是否成功, 给用户的说明文字)。"""
        if not name:
            return False, "没有提供应用名称"
        n = name.strip().strip('"')
        _log(f"open 收到: {n!r}")

        # 网址
        if n.lower().startswith(("http://", "https://")):
            try:
                os.startfile(n)
                return True, f"已在浏览器打开：{n}"
            except Exception as e:
                return False, f"打开网页失败：{e}"

        # 直接路径
        if os.path.exists(n):
            _log(f"  命中直接路径: {n}")
            return self._launch_path(n)

        # 注册表
        exe = _find_app_path(n)
        if exe:
            _log(f"  注册表命中: {exe}")
            return self._launch_path(exe)

        # 快捷方式
        lnk, labels = _find_shortcut(n)
        if lnk:
            _log(f"  命中快捷方式: {lnk}（候选数={len(labels)}）")
            return self._launch_path(lnk)
        else:
            _log(f"  未找到快捷方式。候选: {labels[:20]}")

        # 兜底 start
        try:
            subprocess.Popen(
                f'cmd /c start "" "{n}"',
                shell=True,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            _log(f"  兜底 start 尝试: {n}")
            return True, f"已尝试用 start 打开：{n}"
        except Exception as e:
            _log(f"  兜底 start 失败: {e}")

        # 都没找到：列出可用名
        hint = ""
        if labels:
            uniq = sorted(set(labels))
            hint = "。你桌面上能识别的应用有：" + "、".join(uniq[:30])
        return False, (
            f"没能按名称“{n}”找到程序{hint}。"
            f"你可以直接说上面列出的名字，或告诉我它的安装路径"
            f"（例如 C:\\Program Files\\Tencent\\WeChat\\WeChat.exe），我就能打开它。"
        )


# 全局单例：gui 与 tools 共享同一个，bind_main_thread 才生效
launcher = AppLauncher()
