import sys
import os
import json
import time
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import scrolledtext, messagebox, colorchooser, filedialog, simpledialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG, BASE_DIR
from assistant import Assistant
import tools
from launcher import launcher
from voice import VoiceInput, TTS


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{CONFIG['name']} · 你的 AI 女友")

        # 只保留输入框：无边框、可拖动、半透明悬浮条。其余反馈交给模型气泡/语音。
        root.geometry("470x54")
        root.overrideredirect(True)                       # 去标题栏
        root.attributes("-topmost", CONFIG["input_topmost"])
        try:
            root.attributes("-alpha", float(CONFIG["input_alpha"]))  # 半透明
        except Exception:
            pass

        # 样式（运行时可调，持久化到 data/input_style.json，覆盖 .env 默认值）
        self.style_path = os.path.join(CONFIG["data_dir"], "input_style.json")
        self.style = self.load_style()

        self.bar = tk.Frame(root, padx=4, pady=4)
        self.bar.pack(fill=tk.BOTH, expand=True)

        # 左侧拖动握把（≡），按住可移动整条
        self.grip = tk.Label(self.bar, text="≡", font=("Microsoft YaHei", 12),
                             cursor="fleur", width=2)
        self.grip.pack(side=tk.LEFT, padx=(2, 4))

        # 设置按钮：打开透明度 / 颜色面板（运行时调，不用改 .env）
        self.settings_btn = tk.Button(self.bar, text="◐", command=self.toggle_settings,
                                      font=("Microsoft YaHei", 11), width=2, relief=tk.FLAT)
        self.settings_btn.pack(side=tk.LEFT, padx=(0, 4))

        # 麦克风按钮：点一下开始录音，再点一下结束并识别成文字发送（切换式，比按住更稳）
        self.mic_btn = tk.Button(self.bar, text="🎤", font=("Microsoft YaHei", 11),
                                 width=2, relief=tk.FLAT, command=self._mic_toggle)
        self.mic_btn.pack(side=tk.LEFT, padx=(0, 4))

        # 历史记录按钮：查看与小念的过往聊天记录
        self.history_btn = tk.Button(self.bar, text="📜", font=("Microsoft YaHei", 11),
                                     width=2, relief=tk.FLAT, command=self.view_history)
        self.history_btn.pack(side=tk.LEFT, padx=(0, 4))

        # 记忆 / 检索查看按钮：打开「记忆 / 检索」面板
        self.memory_btn = tk.Button(self.bar, text="🧠", font=("Microsoft YaHei", 11),
                                     width=2, relief=tk.FLAT, command=self.view_memory)
        self.memory_btn.pack(side=tk.LEFT, padx=(0, 4))

        # 语音状态提示（可见，避免报错被藏进隐藏聊天框看不到）
        self.voice_status = tk.Label(self.bar, text="", font=("Microsoft YaHei", 9),
                                     fg="#ffd166", bg=CONFIG["input_bg"])
        self.voice_status.pack(side=tk.LEFT, padx=(0, 4))

        # 输入框：颜色可自定义
        self.entry = tk.Entry(self.bar, font=("Microsoft YaHei", 11),
                              relief=tk.FLAT, highlightthickness=0, bd=0)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        self.entry.bind("<Return>", lambda e: self.send())
        self.entry.focus_set()

        # 发送 / 关闭按钮
        tk.Button(self.bar, text="发送", command=self.send, bg="#ff7fb0", fg="#fff",
                  relief=tk.FLAT, font=("Microsoft YaHei", 10), padx=10
                  ).pack(side=tk.RIGHT, padx=(4, 0))
        self.close_btn = tk.Button(self.bar, text="×", command=self._on_close,
                                   relief=tk.FLAT, font=("Microsoft YaHei", 12), width=2)
        self.close_btn.pack(side=tk.RIGHT, padx=(2, 0))

        self.settings_win = None
        self.console_win = None
        self.memory_win = None
        self.apply_style()   # 应用已保存 / 默认样式

        # 拖动：握把或条上空白区可拖动；点输入框则正常打字
        def on_press(e):
            if e.widget is not self.bar and e.widget is not self.grip:
                return
            root._dx, root._dy = e.x, e.y
            root._drag = True
        def on_drag(e):
            if not getattr(root, "_drag", False):
                return
            x = root.winfo_x() + (e.x - root._dx)
            y = root.winfo_y() + (e.y - root._dy)
            root.geometry(f"+{x}+{y}")
        def on_release(e):
            if getattr(root, "_drag", False):
                root._drag = False
                self._save_pos()
        for w in (self.bar, self.grip):
            w.bind("<Button-1>", on_press)
            w.bind("<B1-Motion>", on_drag)
            w.bind("<ButtonRelease-1>", on_release)

        # 聊天记录区：按需求不显示，仅保留内部接口（append 仍被其它逻辑调用），
        # 小念的回复改由模型动作 + 气泡框 + 后续语音反馈。
        self.chat = scrolledtext.ScrolledText(root)
        self.chat.config(state=tk.DISABLED)

        self.assistant = None
        self.live2d_proc = None
        self._model_ready = False      # 模型是否已完成预热进显存（首条消息前为 False）

        # 受约束自主权限引擎：让小念在白名单内围绕“让生活更好”自调参
        # （只写 data/autonomy_overrides.json，绝不动系统/代码/文件）
        self.autonomy = None
        try:
            from autonomy import Autonomy
            self.autonomy = Autonomy(self)
        except Exception as e:
            self.append("系统", f"自主权限未启动：{e}")

        # 性格情感权重系统：情绪随聊天/行为波动，性格缓慢演变（底层目的不变）
        self.emotion = None
        self.emotion_win = None          # 可视化心情面板的独立窗口
        self.emotion_history = []        # 情绪波动历史（用于起伏曲线）
        try:
            from emotion import EmotionEngine
            self.emotion = EmotionEngine(self)
        except Exception as e:
            self.append("系统", f"情感系统未启动：{e}")

        # 语音：输入(ASR) / 输出(TTS)，配置驱动、可降级
        self.voice = VoiceInput(
            enabled=CONFIG.get("voice_input_enabled", False),
            backend=CONFIG.get("asr_backend", "local"),
            model=CONFIG.get("asr_model", "base"),
            language=CONFIG.get("asr_language", "zh"),
            device=CONFIG.get("asr_device", ""),
        )
        self.tts = TTS(
            enabled=CONFIG.get("voice_output_enabled", False),
            url=CONFIG.get("sovits_url", "http://127.0.0.1:9880"),
            ref_audio=CONFIG.get("sovits_ref_audio", ""),
            ref_text=CONFIG.get("sovits_ref_text", ""),
            speed=CONFIG.get("sovits_speed", 1.0),
            volume=self.style.get("volume", CONFIG["sovits_volume"]),
            if_sr=CONFIG.get("sovits_if_sr", False),
            sample_steps=CONFIG.get("sovits_sample_steps", 8),
            output_device=CONFIG.get("tts_output_device", ""),
        )
        # 应用已保存的音频设备选择（运行时生效，无需重启）
        self.voice.device = self.style.get("input_device", "")
        self.tts.output_device = self.style.get("output_device", "")
        self.sovits_proc = None
        self._apply_voice_state()   # 应用已保存的音色选择（运行时生效，无需重启）

        try:
            self.assistant = Assistant(autonomy=self.autonomy, emotion=self.emotion)
            # 让情感引擎在开启 LLM 感知时用对话模型判断情绪（默认关闭，用关键词规则）
            if self.emotion is not None:
                try:
                    from emotion import set_llm_perceive_fn
                    set_llm_perceive_fn(self.assistant.llm_perceive)
                except Exception:
                    pass
            self._apply_model_state()   # 应用已保存的模型选择
            # 启动预热：后台用原生端点把模型加载进显存并设常驻（Forever），避免首条消息卡加载。
            # 加载期间在输入条显示“模型加载中”，加载完成后才解锁输入——否则首条消息会在
            # 后台冷加载未完成时静默排队、等约 2 分钟才回复（同抢一把 _ollama_lock）。
            self.voice_status.config(text="🧠 模型加载中…")
            def _warmup_and_mark():
                try:
                    self.assistant.warmup()
                finally:
                    self._model_ready = True
                    # 跨线程更新 UI 必须走 after，避免直接操作 tkinter 控件
                    self.root.after(0, lambda: self.show_voice_status("✅ 模型已就绪", 3000))
            threading.Thread(target=_warmup_and_mark, daemon=True).start()
            # 兜底：若 3 分钟内仍未加载完成（如 Ollama 未启动），也解锁输入，
            # 让用户能正常发送（此时走真实错误提示而非无限等待）。
            self.root.after(180000, lambda: setattr(self, "_model_ready", True))
            # 后台保活：周期性把模型钉在显存，避免空闲超过 5 分钟后被 Ollama 卸载导致下次对话卡加载
            self.assistant.start_ollama_keepalive(self.assistant.model)
            self._init_reply_queue()   # 回复队列 + 主动关心计时（用户连续输入串行输出）
            self.start_proactive()     # 并行条件循环：空闲关心 / 软件搭话
            self.start_screen_watch()   # 屏幕活动监控 → 适时给正反馈
            self.start_control_server()  # 接收 live2d 窗口反向指令（输入框显隐）
            self.start_hotkeys()    # 全局快捷键：Ctrl+Alt+V 语音输入 / Ctrl+Alt+G 控制台
            if self.autonomy is not None:
                self.autonomy.start()   # 启动习惯分析线程（受 autonomy_enabled 控制）
            if self.emotion is not None:
                self._start_emotion_loop()   # 启动性格缓慢演变的分析线程
        except RuntimeError as e:
            self.append("系统", str(e))

        # 让“打开软件”等动作在主线程（UI 线程）执行
        launcher.bind_main_thread(lambda fn: self.root.after(0, fn))

        # 启动 IM 接入（QQ/微信）
        if self.assistant is not None:
            try:
                from bot import Bot
                self.bot = Bot(self.assistant).setup()
                self.bot.start()
            except Exception as e:
                self.append("系统", f"IM 接入未启动（不影响本地聊天）：{e}")

        # 启动小念的 Live2D 桌面形象窗口（独立进程，透明桌宠）
        if CONFIG.get("live2d_enabled"):
            self._start_live2d()
            # 启动后让小念用气泡主动打个招呼（等形象窗口 TCP 就绪）
            root.after(2600, lambda: self.live2d_say(
                f"在呢～我是{CONFIG['name']}，你的专属 AI 女友。想聊什么、"
                f"要我帮你开软件查东西，都可以跟我说哦 💕"))

        # 语音输出：若已开启，则拉起（或重建）GPT-SoVITS 推理服务。
        # 每次启动都先清掉 9880 上的旧服务，保证用的是 api_v2 + 八重神子参考音，
        # 不会被残留的老 api.py(v1/仪玄) 卡住导致生成失败。
        if CONFIG.get("voice_output_enabled"):
            threading.Thread(target=self._start_sovits, daemon=True).start()

        # 关闭主窗口时一并结束形象子进程，避免白框残留
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 初始定位：优先用上次记住的位置，否则放屏幕底部居中（仅定位，不改尺寸）
        def _place():
            try:
                p = self.style.get("pos")
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    x, y = int(p[0]), int(p[1])
                else:
                    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
                    x, y = max(0, sw // 2 - 235), max(0, sh - 120)
                root.geometry(f"+{x}+{y}")
            except Exception:
                pass
        root.after(0, _place)

    def append(self, who, text):
        self.chat.config(state=tk.NORMAL)
        self.chat.insert(tk.END, f"{who}：{text}\n\n")
        self.chat.config(state=tk.DISABLED)
        self.chat.see(tk.END)

    # ---------- 聊天记录查看 ----------
    def view_history(self):
        """打开一个窗口，查看小念和你的过往聊天记录。"""
        if self.assistant is None:
            return
        win = tk.Toplevel(self.root)
        win.title(f"{CONFIG['name']} · 聊天记录")
        win.geometry("580x540")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        frm = tk.Frame(win)
        frm.pack(fill=tk.X, padx=6, pady=4)
        tk.Label(frm, text="过往聊天记录（最新在底部）", font=("Microsoft YaHei", 10)).pack(side=tk.LEFT)
        tk.Button(frm, text="复制全部", command=lambda: _copy()).pack(side=tk.RIGHT, padx=4)
        tk.Button(frm, text="刷新", command=lambda: _load()).pack(side=tk.RIGHT, padx=4)

        txt = scrolledtext.ScrolledText(win, font=("Microsoft YaHei", 10), wrap=tk.WORD)
        txt.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        def _load():
            txt.config(state=tk.NORMAL)
            txt.delete("1.0", tk.END)
            hist = self.assistant.memory.data.get("history", [])
            name = CONFIG["name"]
            if not hist:
                txt.insert(tk.END, "还没有聊天记录哦～先跟我聊聊吧💕")
            else:
                for m in hist:
                    role = m.get("role", "")
                    who = "你" if role == "user" else name
                    t = m.get("time", "")
                    content = m.get("content", "")
                    txt.insert(tk.END, f"【{who}】 {t}\n{content}\n\n")
            txt.config(state=tk.DISABLED)
            txt.see(tk.END)

        def _copy():
            rows = self.assistant.memory.data.get("history", [])
            all_text = "\n".join(
                f"[{m.get('role', '')}] {m.get('time', '')}\n{m.get('content', '')}"
                for m in rows
            )
            try:
                win.clipboard_clear()
                win.clipboard_append(all_text)
                self.show_voice_status("已复制全部聊天记录", 2000)
            except Exception:
                pass

        _load()

    # ---------- 记忆 / 检索查看面板 ----------
    def view_memory(self):
        """打开「记忆 / 检索」面板：查看长期记忆摘要、完整归档，并可实时测试 RAG 检索。"""
        if self.assistant is None:
            return
        # 单例：已开则聚焦，避免叠多个窗口
        if getattr(self, "memory_win", None) and self.memory_win.winfo_exists():
            self.memory_win.lift()
            self.memory_win.focus_force()
            return
        mem = self.assistant.memory
        win = tk.Toplevel(self.root)
        self.memory_win = win
        win.title(f"{CONFIG['name']} · 记忆 / 检索")
        win.geometry("660x660")
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        def _on_close():
            setattr(self, "memory_win", None)
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # ===== 顶部：检索测试 =====
        top = tk.Frame(win)
        top.pack(fill=tk.X, padx=6, pady=(6, 2))
        tk.Label(top, text="检索测试：", font=("Microsoft YaHei", 10)).pack(side=tk.LEFT)
        q = tk.Entry(top, font=("Microsoft YaHei", 10))
        q.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        tk.Button(top, text="检索", command=lambda: _retrieve()).pack(side=tk.RIGHT)
        q.bind("<Return>", lambda e: _retrieve())

        res = scrolledtext.ScrolledText(win, font=("Microsoft YaHei", 9),
                                        wrap=tk.WORD, height=7, bg="#fbf7ff")
        res.pack(fill=tk.X, padx=6, pady=(0, 4))
        res.config(state=tk.DISABLED)

        # ===== 操作按钮 + 状态 =====
        btn = tk.Frame(win)
        btn.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Button(btn, text="刷新", command=lambda: _load()).pack(side=tk.LEFT, padx=4)
        tk.Button(btn, text="复制全部", command=lambda: _copy()).pack(side=tk.LEFT)
        stat_var = tk.StringVar(value="")
        tk.Label(btn, textvariable=stat_var, font=("Microsoft YaHei", 9),
                 fg="#8a8a8a").pack(side=tk.LEFT, padx=10)

        # ===== 分栏：左=长期记忆摘要，右=完整归档 =====
        panes = tk.PanedWindow(win, orient=tk.HORIZONTAL, sashrelief=tk.RAISED)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        left = tk.Frame(panes)
        tk.Label(left, text="长期记忆（LLM 压缩摘要）", font=("Microsoft YaHei", 9, "bold")
                  ).pack(anchor="w", padx=4, pady=2)
        sum_txt = scrolledtext.ScrolledText(left, font=("Microsoft YaHei", 9), wrap=tk.WORD)
        sum_txt.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        sum_txt.config(state=tk.DISABLED)

        right = tk.Frame(panes)
        tk.Label(right, text="完整归档（用户+小念，可被检索）",
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor="w", padx=4, pady=2)
        arc_txt = scrolledtext.ScrolledText(right, font=("Microsoft YaHei", 9), wrap=tk.WORD)
        arc_txt.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        arc_txt.config(state=tk.DISABLED)

        panes.add(left, width=320, stretch="always")
        panes.add(right, width=320, stretch="always")

        def _role_name(role):
            return {"user": "你", "assistant": "小念",
                    "summary": "记忆"}.get(role, role)

        def _load():
            # 摘要
            sum_txt.config(state=tk.NORMAL)
            sum_txt.delete("1.0", tk.END)
            summ = mem.data.get("summaries", [])
            if not summ:
                sum_txt.insert(tk.END, "（暂无压缩摘要，聊够 %d 轮后会自动生成）"
                               % int(CONFIG.get("memory_compress_every", 40)))
            else:
                for s in summ:
                    sum_txt.insert(tk.END, f"· {s.get('text', '')}\n\n")
            sum_txt.config(state=tk.DISABLED)

            # 归档
            arc_txt.config(state=tk.NORMAL)
            arc_txt.delete("1.0", tk.END)
            arc = mem.get_archive()
            if not arc:
                arc_txt.insert(tk.END, "（暂无归档记录）")
            else:
                for e in arc:
                    who = _role_name(e.get("role", ""))
                    t = e.get("time", "")
                    content = str(e.get("content", "")).replace("\n", " ")
                    arc_txt.insert(tk.END, f"[{who}] {t}\n{content}\n\n")
            arc_txt.config(state=tk.DISABLED)
            arc_txt.see(tk.END)
            stat_var.set(f"归档 {len(arc)} 条 · 摘要 {len(summ)} 条")

        def _retrieve():
            query = q.get().strip()
            if not query:
                return
            out = mem.retrieve(query, k=CONFIG.get("rag_top_k", 4))
            res.config(state=tk.NORMAL)
            res.delete("1.0", tk.END)
            if out:
                res.insert(tk.END, out)
            else:
                res.insert(tk.END, "（未检索到相关记忆，换个关键词试试～）")
            res.config(state=tk.DISABLED)
            res.see("1.0")

        def _copy():
            parts = []
            for s in mem.data.get("summaries", []):
                parts.append(f"[摘要] {s.get('time', '')}\n{s.get('text', '')}")
            for e in mem.get_archive():
                who = _role_name(e.get("role", ""))
                parts.append(f"[{who}] {e.get('time', '')}\n{e.get('content', '')}")
            all_text = "\n\n".join(parts)
            try:
                win.clipboard_clear()
                win.clipboard_append(all_text)
                self.show_voice_status("已复制全部记忆", 2000)
            except Exception:
                pass

        _load()

    # ---------- 输入条样式：运行时可调 + 持久化 ----------
    def load_style(self):
        default = {
            "alpha": float(CONFIG["input_alpha"]),
            "bg": CONFIG["input_bg"],
            "fg": CONFIG["input_fg"],
            "volume": float(CONFIG["sovits_volume"]),
            "input_device": "",   # 麦克风设备：""=系统默认(优先花再)；或索引/名子串
            "output_device": "",  # 扬声器设备：""=系统默认；或索引/名子串
        }
        try:
            with open(self.style_path, encoding="utf-8") as f:
                saved = json.load(f)
            for k in default:
                if k in saved and saved[k] is not None:
                    default[k] = saved[k]
        except Exception:
            pass
        return default

    def save_style(self):
        try:
            with open(self.style_path, "w", encoding="utf-8") as f:
                json.dump(self.style, f, ensure_ascii=False)
        except Exception:
            pass

    # ---------- 音频设备枚举（麦克风 / 扬声器）----------
    @staticmethod
    def list_audio_devices():
        """枚举声卡设备，返回 (inputs, outputs)，各为 [(idx, name), ...]。

        用 sounddevice 的 query_devices；若 sounddevice 不可用则都返回空列表
        （界面只会显示「系统默认」一项，不影响其它功能）。
        """
        ins, outs = [], []
        try:
            import sounddevice as sd
            for i, d in enumerate(sd.query_devices()):
                name = d.get("name", f"设备{i}")
                if d.get("max_input_channels", 0) > 0:
                    ins.append((i, name))
                if d.get("max_output_channels", 0) > 0:
                    outs.append((i, name))
        except Exception:
            pass
        return ins, outs

    @staticmethod
    def _device_label(idx, devs):
        """根据已存的设备值(索引/名子串/空)反查下拉框应显示的 label。"""
        if not idx:
            return "（系统默认）"
        s = str(idx)
        for i, n in devs:
            if str(i) == s:
                return f"{n} (#{i})"
        # 存的是名子串的情况：按子串模糊匹配
        for i, n in devs:
            if s and s.lower() in n.lower():
                return f"{n} (#{i})"
        return "（系统默认）"

    @staticmethod
    def _device_value(label):
        """把下拉框 label 解析回设备值：默认返回 ''，否则返回索引字符串。"""
        if not label or label == "（系统默认）":
            return ""
        h = label.rfind("#")
        if h == -1:
            return label.strip()
        return label[h + 1:].rstrip(")")

    # ---------- 模型 / 音色 切换（运行时生效 + 持久化）----------
    def _models_path(self):
        return os.path.join(CONFIG["data_dir"], "models.json")

    def _load_model_state(self):
        default = {"current": CONFIG["model"], "models": list(CONFIG["models"])}
        try:
            with open(self._models_path(), encoding="utf-8") as f:
                s = json.load(f)
            if s.get("current"):
                default["current"] = s["current"]
            if isinstance(s.get("models"), list) and s["models"]:
                default["models"] = s["models"]
        except Exception:
            pass
        return default

    def _save_model_state(self, current, models):
        try:
            with open(self._models_path(), "w", encoding="utf-8") as f:
                json.dump({"current": current, "models": models}, f, ensure_ascii=False)
        except Exception:
            pass

    def _apply_model_state(self):
        if self.assistant is None:
            return
        self.assistant.model = self._load_model_state()["current"]

    def _on_model_change(self, value, models):
        if self.assistant is not None:
            self.assistant.model = value
        self._save_model_state(value, models)
        self.show_voice_status(f"🤖 已切换模型：{value}", 3000)

    # ---------- API 设置（运行时更换服务商 / 密钥 / 模型，无需重启）----------
    def _write_env_value(self, key, value):
        """把某个配置写回 .env（保留其它行与注释），下次启动仍生效。"""
        path = os.path.join(BASE_DIR, ".env")
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        out, replaced = [], False
        for ln in lines:
            if ln.startswith(key + "=") or ln.startswith(key + " ="):
                out.append(f"{key}={value}\n")
                replaced = True
            else:
                out.append(ln)
        if not replaced:
            out.append(f"{key}={value}\n")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(out)
            return True
        except Exception:
            return False

    def _build_api_section(self, parent):
        """在任意面板里插入「API 设置」分区：Key + Base URL + 模型 + 测试/保存。"""
        tk.Label(parent, text="API 设置（更换服务商 / 密钥 / 模型）",
                 font=("Microsoft YaHei", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 2))

        tk.Label(parent, text="API Key", anchor="w").pack(fill=tk.X, padx=12, pady=(4, 0))
        key_var = tk.StringVar(value=CONFIG.get("api_key", ""))
        key_entry = tk.Entry(parent, textvariable=key_var, width=34, show="*")
        key_entry.pack(padx=12, fill=tk.X)
        show_var = tk.BooleanVar(value=False)
        def _toggle_show():
            key_entry.config(show="" if show_var.get() else "*")
        tk.Checkbutton(parent, text="显示密钥", variable=show_var,
                       command=_toggle_show).pack(anchor="w", padx=12)

        tk.Label(parent, text="Base URL（接口地址，如 https://api.deepseek.com/v1）",
                 anchor="w").pack(fill=tk.X, padx=12, pady=(6, 0))
        url_var = tk.StringVar(value=CONFIG.get("base_url", ""))
        tk.Entry(parent, textvariable=url_var, width=34).pack(padx=12, fill=tk.X)

        tk.Label(parent, text="模型名（如 deepseek-chat / gpt-4o-mini / 本地模型名）",
                 anchor="w").pack(fill=tk.X, padx=12, pady=(6, 0))
        model_var = tk.StringVar(value=CONFIG.get("model", ""))
        tk.Entry(parent, textvariable=model_var, width=34).pack(padx=12, fill=tk.X)

        row = tk.Frame(parent)
        row.pack(padx=12, pady=(8, 4))
        tk.Button(row, text="测试连接", width=12,
                  command=lambda: self._on_api_test(key_var, url_var, model_var)
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(row, text="保存并应用", width=12,
                  command=lambda: self._on_api_save(key_var, url_var, model_var)
                  ).pack(side=tk.LEFT, padx=4)

    def _on_api_test(self, key_var, url_var, model_var):
        key = key_var.get().strip()
        url = url_var.get().strip()
        model = model_var.get().strip() or CONFIG.get("model", "")
        if not key or not url:
            self.show_voice_status("请先填写 API Key 和 Base URL", 4000)
            return
        self.show_voice_status("正在测试连接…", 2000)

        def _run():
            try:
                from openai import OpenAI
                c = OpenAI(api_key=key, base_url=url)
                r = c.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=8,
                )
                txt = (r.choices[0].message.content or "").strip()
                self.root.after(0, lambda: self.show_voice_status(
                    f"✅ 连接成功：{(txt[:24] or '空回复')!r}", 5000))
            except Exception as e:
                msg = str(e)
                if len(msg) > 220:
                    msg = msg[:220] + "…"
                self.root.after(0, lambda: self.show_voice_status(
                    f"❌ 连接失败：{msg}", 9000))
        threading.Thread(target=_run, daemon=True).start()

    def _on_api_save(self, key_var, url_var, model_var):
        key = key_var.get().strip()
        url = url_var.get().strip()
        model = model_var.get().strip() or CONFIG.get("model", "")
        if not key or not url:
            self.show_voice_status("⚠ API Key 和 Base URL 不能为空", 4000)
            return
        # 热替换客户端（无需重启）
        if self.assistant is not None:
            try:
                self.assistant.set_api(api_key=key, base_url=url, model=model)
            except Exception as e:
                self.show_voice_status(f"⚠ 应用失败：{e}", 6000)
                return
        # 持久化到 .env，下次启动仍生效
        self._write_env_value("OPENAI_API_KEY", key)
        self._write_env_value("OPENAI_BASE_URL", url)
        self._write_env_value("MODEL", model)
        # 同步模型下拉（models.json），让“对话模型”下拉也能反映新模型
        mst = self._load_model_state()
        models = list(mst["models"])
        if model and model not in models:
            models.append(model)
        self._save_model_state(model, models)
        self.show_voice_status(f"✅ 已更换 API：{model} @ {url}", 5000)

    # ---------- 视觉（看屏）API 设置：GLM-4V-Flash 等，可独立更换 ----------
    def _build_vision_api_section(self, parent):
        """在任意面板里插入「视觉 API 设置」分区：启用 + Key + Base URL + 模型 + 测试/保存。"""
        tk.Label(parent, text="视觉 API（看懂屏幕 · GLM-4V-Flash 等，独立于对话）",
                 font=("Microsoft YaHei", 10, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 2))

        en_var = tk.BooleanVar(value=bool(CONFIG.get("vision_enabled")))
        tk.Checkbutton(parent, text="启用视觉（让小念看懂屏幕）", variable=en_var,
                       command=lambda: self._on_vision_enable(en_var)
                       ).pack(anchor="w", padx=12, pady=(2, 4))

        tk.Label(parent, text="视觉 API Key（如智谱 GLM-4V-Flash 的 key）",
                 anchor="w").pack(fill=tk.X, padx=12, pady=(4, 0))
        vkey_var = tk.StringVar(value=CONFIG.get("vision_api_key", ""))
        vkey_entry = tk.Entry(parent, textvariable=vkey_var, width=34, show="*")
        vkey_entry.pack(padx=12, fill=tk.X)
        vshow_var = tk.BooleanVar(value=False)
        tk.Checkbutton(parent, text="显示密钥", variable=vshow_var,
                       command=lambda: vkey_entry.config(show="" if vshow_var.get() else "*")
                       ).pack(anchor="w", padx=12)

        tk.Label(parent, text="视觉 Base URL（如 https://open.bigmodel.cn/api/paas/v4）",
                 anchor="w").pack(fill=tk.X, padx=12, pady=(6, 0))
        vurl_var = tk.StringVar(value=CONFIG.get("vision_base_url", "https://open.bigmodel.cn/api/paas/v4"))
        tk.Entry(parent, textvariable=vurl_var, width=34).pack(padx=12, fill=tk.X)

        tk.Label(parent, text="视觉模型名（如 glm-4v-flash / glm-4v / gpt-4o）",
                 anchor="w").pack(fill=tk.X, padx=12, pady=(6, 0))
        vmodel_var = tk.StringVar(value=CONFIG.get("vision_model", "glm-4v-flash"))
        tk.Entry(parent, textvariable=vmodel_var, width=34).pack(padx=12, fill=tk.X)

        row = tk.Frame(parent)
        row.pack(padx=12, pady=(8, 4))
        tk.Button(row, text="测试连接", width=12,
                  command=lambda: self._on_vision_api_test(vkey_var, vurl_var, vmodel_var)
                  ).pack(side=tk.LEFT, padx=4)
        tk.Button(row, text="保存并应用", width=12,
                  command=lambda: self._on_vision_api_save(vkey_var, vurl_var, vmodel_var)
                  ).pack(side=tk.LEFT, padx=4)

    def _on_vision_enable(self, en_var):
        val = "true" if en_var.get() else "false"
        self._write_env_value("VISION_ENABLED", val)
        CONFIG["vision_enabled"] = en_var.get()
        self.show_voice_status(f"视觉已{'启用' if en_var.get() else '关闭'}（下次看懂屏幕时生效）", 3500)

    def _on_vision_api_test(self, key_var, url_var, model_var):
        key = key_var.get().strip()
        url = url_var.get().strip()
        model = model_var.get().strip() or CONFIG.get("vision_model", "glm-4v-flash")
        if not key or not url:
            self.show_voice_status("请先填写视觉 API Key 和 Base URL", 4000)
            return
        self.show_voice_status("正在测试视觉连接…", 2000)
        # 1x1 透明 PNG，用于让视觉模型真正走图像接口验证连通性
        PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
               "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

        def _run():
            try:
                from openai import OpenAI
                c = OpenAI(api_key=key, base_url=url)
                r = c.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "ping"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                    ]}],
                    max_tokens=8,
                )
                txt = (r.choices[0].message.content or "").strip()
                self.root.after(0, lambda: self.show_voice_status(
                    f"✅ 视觉连接成功：{(txt[:24] or '空回复')!r}", 5000))
            except Exception as e:
                msg = str(e)
                if len(msg) > 220:
                    msg = msg[:220] + "…"
                self.root.after(0, lambda: self.show_voice_status(
                    f"❌ 视觉连接失败：{msg}", 9000))
        threading.Thread(target=_run, daemon=True).start()

    def _on_vision_api_save(self, key_var, url_var, model_var):
        key = key_var.get().strip()
        url = url_var.get().strip()
        model = model_var.get().strip() or CONFIG.get("vision_model", "glm-4v-flash")
        if not key or not url:
            self.show_voice_status("⚠ 视觉 API Key 和 Base URL 不能为空", 4000)
            return
        try:
            import vision
            vision.set_vision_api(api_key=key, base_url=url, model=model)
        except Exception as e:
            self.show_voice_status(f"⚠ 视觉应用失败：{e}", 6000)
            return
        self._write_env_value("VISION_API_KEY", key)
        self._write_env_value("VISION_BASE_URL", url)
        self._write_env_value("VISION_MODEL", model)
        self.show_voice_status(f"✅ 已更换视觉 API：{model} @ {url}", 5000)

    def _voices_path(self):
        return os.path.join(CONFIG["data_dir"], "voices.json")

    def _default_voices(self):
        """预设音色：当前默认(八重神子) + 声音素材里的另一份(仪玄)。"""
        voices = {}
        cur_audio = CONFIG.get("sovits_ref_audio", "")
        cur_text = CONFIG.get("sovits_ref_text", "")
        if cur_audio:
            voices["八重神子"] = {"ref_audio": cur_audio, "ref_text": cur_text}
        # 仪玄：声音素材目录里的另一份参考音（原始 mp3，可能略带背景音；
        # 想要更干净可用 prep_ref_audio.py 处理成 wav 后替换 ref_audio）
        yixuan_dir = os.path.join(os.path.dirname(BASE_DIR), "声音素材", "仪玄")
        try:
            mp3s = [f for f in os.listdir(yixuan_dir) if f.lower().endswith(".mp3")]
        except Exception:
            mp3s = []
        txt_path = os.path.join(
            yixuan_dir,
            "以量取胜，真无趣。无妨，再清理一遍便是。随便清理一下吧。真是没完没了啊。.txt")
        yixuan_text = ""
        try:
            with open(txt_path, encoding="utf-8") as f:
                yixuan_text = f.read().strip()
        except Exception:
            pass
        if mp3s:
            voices["仪玄"] = {"ref_audio": os.path.join(yixuan_dir, mp3s[0]),
                              "ref_text": yixuan_text}
        return voices

    def _load_voice_state(self):
        voices = self._default_voices()
        current = "八重神子" if "八重神子" in voices else next(iter(voices), None)
        try:
            with open(self._voices_path(), encoding="utf-8") as f:
                s = json.load(f)
            if isinstance(s.get("presets"), dict):
                merged = dict(voices)
                merged.update(s["presets"])   # 保留用户新增的音色
                voices = merged
            if s.get("current") in voices:
                current = s["current"]
        except Exception:
            pass
        return {"current": current, "presets": voices}

    def _save_voice_state(self, current, presets):
        try:
            with open(self._voices_path(), "w", encoding="utf-8") as f:
                json.dump({"current": current, "presets": presets}, f, ensure_ascii=False)
        except Exception:
            pass

    def _apply_voice_state(self):
        st = self._load_voice_state()
        v = st["presets"].get(st["current"])
        if v and getattr(self, "tts", None) is not None:
            self.tts.ref_audio = v.get("ref_audio", "")
            self.tts.ref_text = v.get("ref_text", "")

    def _on_voice_change(self, value, presets):
        v = presets.get(value)
        if not v:
            return
        if getattr(self, "tts", None) is not None:
            self.tts.ref_audio = v.get("ref_audio", "")
            self.tts.ref_text = v.get("ref_text", "")
        self._save_voice_state(value, presets)
        # 嘴型/动作适配：该音色若关联了 Live2D 形象，切语音时同步切形象，
        # 让嘴型参数(ParamMouthOpenY)等动作与新形象匹配。
        mdl = v.get("live2d_model")
        if mdl:
            ld_map = {n: r for n, r in self._discover_live2d_models()}
            label = next((n for n, r in ld_map.items() if r == mdl), None)
            self._switch_live2d_model(mdl, label=label)
        self.show_voice_status(f"🎙 已切换音色：{value}", 3000)

    # ---------- 添加音色（弹窗：名称 + 音频 + 文字，可选关联形象）----------
    def _add_voice_dialog(self):
        """弹窗录入一个新音色并写进 data/voices.json（无需手改 json）。

        可选「关联形象」：选中该音色时会自动切到对应 Live2D 模型，
        使嘴型/动作适配（前提是该模型嘴型参数名为 ParamMouthOpenY）。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("添加音色")
        dlg.resizable(False, False)
        try:
            dlg.transient(self.root)
            dlg.grab_set()
        except Exception:
            pass

        tk.Label(dlg, text="音色名称").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 0))
        name_var = tk.StringVar()
        tk.Entry(dlg, textvariable=name_var, width=30).grid(row=0, column=1, padx=10, pady=(10, 0))

        tk.Label(dlg, text="参考音频").grid(row=1, column=0, sticky="w", padx=10, pady=(6, 0))
        audio_var = tk.StringVar()
        tk.Entry(dlg, textvariable=audio_var, width=30).grid(row=1, column=1, padx=10, pady=(6, 0))
        tk.Button(dlg, text="浏览…", command=lambda: audio_var.set(
            filedialog.askopenfilename(
                title="选择参考音频",
                filetypes=[("音频", "*.wav *.mp3 *.flac *.ogg *.m4a"), ("全部", "*.*")])
        )).grid(row=1, column=2, padx=(0, 10), pady=(6, 0))

        tk.Label(dlg, text="对应文字").grid(row=2, column=0, sticky="nw", padx=10, pady=(6, 0))
        txt = tk.Text(dlg, width=32, height=3)
        txt.grid(row=2, column=1, columnspan=2, padx=10, pady=(6, 0))

        tk.Label(dlg, text="关联形象").grid(row=3, column=0, sticky="w", padx=10, pady=(6, 0))
        ld_map = {n: r for n, r in self._discover_live2d_models()}
        model_names = ["（不关联）"] + list(ld_map.keys())
        model_var = tk.StringVar(value="（不关联）")
        tk.OptionMenu(dlg, model_var, *model_names).grid(row=3, column=1, columnspan=2, sticky="w", padx=10, pady=(6, 0))

        def _confirm():
            name = name_var.get().strip()
            audio = audio_var.get().strip()
            text = txt.get("1.0", "end").strip()
            model = model_var.get()
            if not name:
                messagebox.showerror("缺少名称", "请填写音色名称")
                return
            if not audio or not os.path.isfile(audio):
                messagebox.showerror("音频无效", "请选择有效的参考音频文件")
                return
            if not text:
                messagebox.showerror("缺少文字", "请填写参考音频对应的文字")
                return
            st = self._load_voice_state()
            presets = dict(st["presets"])
            if name in presets and not messagebox.askyesno("已存在", f"音色「{name}」已存在，是否覆盖？"):
                return
            preset = {"ref_audio": os.path.abspath(audio), "ref_text": text}
            if model and model != "（不关联）":
                preset["live2d_model"] = ld_map.get(model, "")
            presets[name] = preset
            self._save_voice_state(name, presets)
            self._apply_voice_state()
            # 若关联了形象，立即切过去让嘴型/动作适配
            if preset.get("live2d_model"):
                self._switch_live2d_model(preset["live2d_model"], label=model)
            self._refresh_voice_menu()
            dlg.destroy()
            self.show_voice_status(f"🎙 已添加音色：{name}", 4000)

        tk.Button(dlg, text="确定", command=_confirm, width=10)\
            .grid(row=4, column=1, sticky="e", padx=10, pady=(10, 6))
        tk.Button(dlg, text="取消", command=dlg.destroy, width=10)\
            .grid(row=4, column=2, sticky="w", padx=10, pady=(10, 6))

    def _refresh_voice_menu(self):
        """重建设置面板里的「语音音色」下拉（新增音色后即时刷新选项）。"""
        frame = getattr(self, "_voice_menu_frame", None)
        if frame is None:
            return
        for child in list(frame.children.values()):
            try:
                child.destroy()
            except Exception:
                pass
        st = self._load_voice_state()
        presets = st["presets"]
        names = list(presets.keys()) or ["（无可用音色）"]
        var = tk.StringVar(value=st["current"])
        menu = tk.OptionMenu(
            frame, var, *names,
            command=lambda v: self._on_voice_change(v, presets))
        menu.config(width=20)
        menu.pack(fill=tk.X)
        self._voice_menu = menu

    # ---------- Live2D 形象切换（运行时生效 + 持久化到 .env LIVE2D_MODEL）----------
    def _discover_live2d_models(self):
        """扫描可用 Live2D 模型（assets/live2d 下所有 *.model3.json）。返回 [(显示名, 相对项目根路径)]。"""
        found = []
        adir = os.path.join(BASE_DIR, "assets", "live2d")
        if os.path.isdir(adir):
            for dp, _, files in os.walk(adir):
                for f in files:
                    if f.lower().endswith(".model3.json"):
                        ap = os.path.abspath(os.path.join(dp, f))
                        rel = os.path.relpath(ap, BASE_DIR).replace(os.sep, "/")
                        name = f[: -len(".model3.json")]
                        found.append((name, rel))
        seen, uniq = set(), []
        for name, rel in found:
            if rel not in seen:
                seen.add(rel)
                uniq.append((name, rel))
        return uniq

    def _switch_live2d_model(self, rel, label=None):
        """切换 Live2D 形象（写回 .env + 通知前端重新加载）。"""
        if not rel:
            return
        # 记忆上次选择（即使形象未运行也能记住）
        try:
            with open(os.path.join(CONFIG["data_dir"], "live2d_model.json"),
                      "w", encoding="utf-8") as f:
                json.dump({"current": rel}, f, ensure_ascii=False)
        except Exception:
            pass
        if not CONFIG.get("live2d_enabled"):
            return
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                s.sendall(json.dumps({"switch_live2d_model": rel}).encode("utf-8"))
        except Exception:
            pass
        if label:
            self.show_voice_status(f"🧸 已切换形象：{label}", 3000)

    def _on_live2d_model_change(self, value, models):
        rel = models.get(value)
        if not rel:
            return
        self._switch_live2d_model(rel, label=value)

    def _save_pos(self):
        try:
            self.style["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.save_style()
        except Exception:
            pass

    def apply_style(self):
        s = self.style
        try:
            self.root.attributes("-alpha", float(s["alpha"]))
        except Exception:
            pass
        try:
            self.bar.config(bg=s["bg"])
            self.grip.config(bg=s["bg"], fg=s["fg"])
            self.entry.config(bg=s["bg"], fg=s["fg"], insertbackground=s["fg"])
            self.settings_btn.config(bg=s["bg"], fg=s["fg"])
            self.close_btn.config(bg=s["bg"], fg=s["fg"])
        except Exception:
            pass

    def toggle_settings(self):
        if getattr(self, "settings_win", None) and self.settings_win.winfo_exists():
            self.settings_win.destroy()
            self.settings_win = None
            return
        self.open_settings()

    def open_settings(self):
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("输入条设置")
        win.attributes("-topmost", True)
        # 允许双向拉伸（横向拉宽时内部控件跟随撑开，纵向拉高配合滚动条）
        win.resizable(True, True)
        try:
            x = self.root.winfo_x() + 20
            y = self.root.winfo_y() - 170
            if y < 0:
                y = 0
            win.geometry(f"260x540+{x}+{y}")
        except Exception:
            pass

        # ---- 滚动容器：Canvas + 右侧 Scrollbar，内容超长时可滚动查看 ----
        canvas = tk.Canvas(win, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(win, orient=tk.VERTICAL, command=canvas.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=scrollbar.set)

        body = tk.Frame(canvas)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        # 宽度跟随画布：窗口拉宽时，让内层 body 同步变宽，fill=tk.X 的控件才会撑开
        def _on_canvas_configure(e):
            canvas.itemconfig(body_id, width=e.width)
            canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", _on_canvas_configure)
        # 内层内容变化时也刷新滚动区域
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        # 鼠标滚轮滚动（窗口关闭时解绑，避免影响其它窗口）
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        tk.Label(body, text="透明度").pack(anchor="w", padx=12, pady=(10, 0))
        alpha_var = tk.DoubleVar(value=self.style["alpha"])
        tk.Scale(body, from_=0.3, to=1.0, resolution=0.01, orient=tk.HORIZONTAL,
                 variable=alpha_var, length=220,
                 command=lambda v: self._set_alpha(float(v))).pack(padx=10)

        tk.Label(body, text="语音音量").pack(anchor="w", padx=12, pady=(6, 0))
        vol_var = tk.DoubleVar(value=self.style["volume"])
        tk.Scale(body, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL,
                 variable=vol_var, length=220,
                 command=lambda v: self._set_volume(float(v))).pack(padx=10)

        # 降噪实时开关：直接改 tts.if_sr(=super_sampling)，下次朗读生效，无需重启
        denoise_var = tk.BooleanVar(value=bool(self.tts.if_sr))
        tk.Checkbutton(
            body, text="✓ 语音降噪（更干净/略慢）", variable=denoise_var,
            command=lambda: self._set_denoise(denoise_var.get()),
        ).pack(anchor="w", padx=12, pady=(6, 0))

        # ---- 音频输入/输出设备选择（运行时生效，持久化到 input_style.json）----
        ins, outs = self.list_audio_devices()
        in_labels = ["（系统默认）"] + [f"{n} (#{i})" for i, n in ins]
        out_labels = ["（系统默认）"] + [f"{n} (#{i})" for i, n in outs]

        tk.Label(body, text="麦克风（语音输入）").pack(anchor="w", padx=12, pady=(8, 0))
        in_var = tk.StringVar(value=self._device_label(self.style.get("input_device", ""), ins))
        in_menu = tk.OptionMenu(
            body, in_var, *in_labels,
            command=lambda v: self._on_input_device_change(v))
        in_menu.config(width=24)
        in_menu.pack(padx=10, fill=tk.X)

        tk.Label(body, text="扬声器（语音输出）").pack(anchor="w", padx=12, pady=(6, 0))
        out_var = tk.StringVar(value=self._device_label(self.style.get("output_device", ""), outs))
        out_menu = tk.OptionMenu(
            body, out_var, *out_labels,
            command=lambda v: self._on_output_device_change(v))
        out_menu.config(width=24)
        out_menu.pack(padx=10, fill=tk.X)

        # ---- 对话模型切换（运行时生效，持久化到 data/models.json）----
        tk.Label(body, text="对话模型").pack(anchor="w", padx=12, pady=(10, 0))
        mst = self._load_model_state()
        model_var = tk.StringVar(value=mst["current"])
        model_menu = tk.OptionMenu(
            body, model_var, *mst["models"],
            command=lambda v: self._on_model_change(v, mst["models"]))
        model_menu.config(width=22)
        model_menu.pack(padx=10, fill=tk.X)

        # ---- API 设置（运行时更换服务商 / 密钥 / 模型）----
        self._build_api_section(body)

        # ---- 视觉 API 设置（运行时更换 GLM-4V-Flash 等看屏模型的 Key/URL/模型）----
        self._build_vision_api_section(body)

        # ---- 语音音色切换（运行时生效，持久化到 data/voices.json）----
        tk.Label(body, text="语音音色").pack(anchor="w", padx=12, pady=(10, 0))
        self._voice_menu_frame = tk.Frame(body)
        self._voice_menu_frame.pack(padx=10, fill=tk.X)
        self._refresh_voice_menu()
        tk.Button(
            body, text="➕ 添加音色", command=self._add_voice_dialog,
        ).pack(anchor="e", padx=12, pady=(2, 0))

        # ---- Live2D 形象切换（运行时生效，写回 .env LIVE2D_MODEL）----
        tk.Label(body, text="Live2D 形象").pack(anchor="w", padx=12, pady=(10, 0))
        ld_models = self._discover_live2d_models()
        ld_map = {n: r for n, r in ld_models}
        ld_names = list(ld_map.keys()) or ["（无可用模型）"]
        cur_rel = CONFIG.get("live2d_model", "")
        ld_sel = next((n for n, r in ld_models if r == cur_rel), ld_names[0])
        ld_var = tk.StringVar(value=ld_sel)
        ld_menu = tk.OptionMenu(
            body, ld_var, *ld_names,
            command=lambda v: self._on_live2d_model_change(v, ld_map))
        ld_menu.config(width=22)
        ld_menu.pack(padx=10, fill=tk.X)

        row = tk.Frame(body)
        row.pack(pady=(4, 10))
        tk.Button(row, text="背景色", command=lambda: self._pick_color("bg"),
                  width=8).pack(side=tk.LEFT, padx=6)
        tk.Button(row, text="文字色", command=lambda: self._pick_color("fg"),
                  width=8).pack(side=tk.LEFT, padx=6)

        def _on_close():
            canvas.unbind_all("<MouseWheel>")
            win.destroy()
            setattr(self, "settings_win", None)
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _set_alpha(self, v):
        self.style["alpha"] = v
        try:
            self.root.attributes("-alpha", v)
        except Exception:
            pass
        self.save_style()

    def _set_volume(self, v):
        self.style["volume"] = v
        if getattr(self, "tts", None) is not None:
            self.tts.volume = v
        self.save_style()

    def _set_denoise(self, v):
        # 实时切换降噪（super_sampling），下次 speak 生效
        if getattr(self, "tts", None) is not None:
            self.tts.if_sr = bool(v)

    def _on_input_device_change(self, label):
        """麦克风设备切换：立刻生效（下次录音使用），并持久化到 style。"""
        dev = self._device_value(label)
        self.style["input_device"] = dev
        if getattr(self, "voice", None) is not None:
            self.voice.device = dev
        self.save_style()
        if dev == "":
            self.show_voice_status("麦克风：已切回系统默认")
        else:
            self.show_voice_status(f"麦克风：已切换到 {label}")

    def _on_output_device_change(self, label):
        """扬声器设备切换：立刻生效（下次播放使用），并持久化到 style。"""
        dev = self._device_value(label)
        self.style["output_device"] = dev
        if getattr(self, "tts", None) is not None:
            self.tts.output_device = dev
        self.save_style()
        if dev == "":
            self.show_voice_status("扬声器：已切回系统默认")
        else:
            self.show_voice_status(f"扬声器：已切换到 {label}")

    def _pick_color(self, key):
        _, color = colorchooser.askcolor(initialcolor=self.style[key])
        if not color:
            return
        self.style[key] = color
        self.apply_style()
        self.save_style()

    def send(self):
        if self.assistant is None:
            return
        # 模型尚未预热完成时，首条消息会卡在冷加载上（约 1~2 分钟）。直接提示并放弃本次发送，
        # 等输入条显示“✅ 模型已就绪”再聊，即可秒回，避免无声等待。
        if not self._model_ready:
            self.show_voice_status("小念还在加载模型，稍等十几秒哦～", 3000)
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, tk.END)
        self.append("你", text)
        # 改为入队：用户连续快速输入时，按输入先后依次生成并播报回复，
        # 避免多条回复并发抢语音、互相打断。
        self._enqueue_user(text)

    def _reply_one(self, text):
        # 用户说“跳/转身”等指令时，立即触发对应动作（不等回复）
        self._send_live2d_action(self._detect_action(text))

        def on_tool(name, args, result):
            if name == "go_sleep":
                # 睡眠机制：标记待关闭，等晚安 TTS 播完后在 _reply_one 末尾优雅关闭
                self._pending_sleep = True
                self.root.after(0, lambda: self.append("💤 小念", "收到～我去睡啦，明天见💕"))
                return
            self.root.after(0, lambda: self.append(f"🛠 {name}", str(result)[:500]))

        # 流式输出：先开气泡头，on_token 实时把文本增量追加到同一气泡；
        # 整句生成完后再做语音合成（TTS 等整句，口型/动作对齐）。
        self.root.after(0, lambda: self.chat.insert(tk.END, f"{CONFIG['name']}："))

        def on_token(piece):
            self.root.after(0, lambda p=piece: self._stream_append(p))

        try:
            reply = self.assistant.chat(text, on_tool=on_tool, on_token=on_token)
        except Exception as e:
            self.root.after(0, lambda e=e: self.append("出错了", str(e)))
            return
        # 气泡收尾：补换行 + 滚动到底
        self.root.after(0, self._stream_end)
        # 语音输出：同步播放，worker 会等它播完再处理下一条，
        # 保证“按输入先后依次输出”，且发声与口型/动作对齐。
        if self.tts.is_ready():
            self._speak(reply)
        elif CONFIG.get("voice_output_enabled"):
            self.root.after(0, lambda: self.show_voice_status(
                "语音输出未配置：请在 .env 填 SOVITS_*", 6000))
        # 睡眠机制：若本条对话触发了“去睡”，等晚安 TTS 播完后再优雅关闭程序
        if getattr(self, "_pending_sleep", False):
            self._pending_sleep = False
            self.root.after(2000, lambda: self._on_close())

    def _stream_append(self, piece):
        """流式回复时，把增量文本追加到当前小念气泡（主线程调用）。"""
        self.chat.insert(tk.END, piece)
        self.chat.see(tk.END)

    def _stream_end(self):
        """一条流式回复结束：补换行并滚动到底（主线程调用）。"""
        self.chat.insert(tk.END, "\n\n")
        self.chat.see(tk.END)

    def _show_reply_text(self, who, text):
        """仅显示回复文字（兼容旧调用；现主回复走流式 _reply_one）。"""
        self.append(who, text)

    def _show_reply(self, who, text, proactive=False):
        """显示一条回复文字（并异步播报）。

        proactive=True 表示这是小念“主动”说的内容（看屏正反馈 / 主动关心）。
        优先级规则：当用户正在被回复、或还有未处理的用户提问时，主动内容让位——
        不输出（既不显示气泡也不播报），优先保证用户提问的回复。
        """
        if proactive and self._user_active():
            return
        self.append(who, text)
        if self.tts.is_ready():
            threading.Thread(target=self._speak, args=(text,), daemon=True).start()
        elif CONFIG.get("voice_output_enabled"):
            self.show_voice_status("语音输出未配置：请在 .env 填 SOVITS_*", 6000)

    def _speak(self, text):
        # 若语音合成服务没运行，先自动拉起（比如你关过窗口、子进程被终止）
        if not self._sovits_up():
            self.show_voice_status("语音合成服务未运行，正在启动…稍候", 4000)
            threading.Thread(target=self._start_sovits, daemon=True).start()
        name = CONFIG["name"]
        # on_play：音频开始播放时，气泡+说话动作+进入实时口型模式（与声音起点对齐）
        def on_play():
            # 取当前主导情绪，传给前端让动作/表情也随心情变化（动作自适应情绪）
            dom = None
            emo = getattr(self, "emotion", None)
            if emo is not None:
                try:
                    dom = emo.dominant()
                except Exception:
                    dom = None
            self.live2d_say(text, name, start_talk=True, emotion=dom)
        # on_level：播放期间每帧把当前音频能量传过去，驱动嘴型实时张合（长句也同步）
        def on_level(rms):
            self._send_mouth(rms)
        # 串行锁：保证所有语音输出（用户回复 / 看屏 / 关心）互不重叠、口型对齐
        with self._speak_lock:
            err = self.tts.speak(text, on_play=on_play, on_level=on_level)
            # 无论成功或失败，播放结束后都归位（口型归零、淡出气泡），避免卡在“张嘴”状态
            self._stop_talk()
        if err:
            self.show_voice_status("🔊 " + err, 6000)

    # ---------- 可见状态提示 ----------
    def show_voice_status(self, msg, ms=3000):
        self.voice_status.config(text=msg)
        self.root.after(ms, lambda: self.voice_status.config(text=""))

    # ---------- 麦克风语音输入（点击切换）----------
    def _mic_toggle(self):
        if not CONFIG.get("voice_input_enabled"):
            self.show_voice_status("语音输入未开启：.env 设 VOICE_INPUT_ENABLED=true", 6000)
            return
        if getattr(self.voice, "_recording", False):
            # 第二次点击：停止并识别
            self.mic_btn.config(text="🎤", relief=tk.FLAT)
            self.show_voice_status("识别中…", 2000)
            threading.Thread(target=self._mic_recognize, daemon=True).start()
        else:
            # 第一次点击：开始录音
            if self.voice.start():
                self.mic_btn.config(text="⏹", relief=tk.SUNKEN)
                dev_name = getattr(self.voice, "last_device_name", "")
                suffix = f"（设备：{dev_name}）" if dev_name else ""
                self.show_voice_status("🎤 聆听中…再点一下结束" + suffix, 5000)
            else:
                self.show_voice_status("无法打开麦克风，检查设备/权限", 6000)

    def _mic_recognize(self):
        text, level = self.voice.stop()
        if text.startswith("__VOICE_ERR__"):
            msg = "语音识别出错：" + text[len("__VOICE_ERR__"):]
            self.show_voice_status("⚠ " + msg, 8000)
            self.append("系统", "🎤 " + msg)
            return
        if not text:
            if level < 0.005:
                self.append("系统",
                    "🎤 没听到声音：可能是麦克风被其它程序占用，或选错了设备。"
                    "请到设置面板(◐)的「麦克风」下拉里确认选的是你正在说话的那只麦克风。")
                self.show_voice_status("没听到声音，再试一次", 4000)
            else:
                self.append("系统",
                    f"🎤 听到了声音(音量{level:.3f})但没识别出文字，请靠近麦、说清楚一点，"
                    "或到设置面板(◐)换一个麦克风设备试试。")
                self.show_voice_status(f"没识别到文字(音量{level:.3f})，靠近麦/大声点", 5000)
            return
        self.root.after(0, lambda: self._send_voice_text(text))

    def _send_voice_text(self, text):
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        if CONFIG.get("voice_confirm_send", True):
            # 回显确认模式：识别结果先填入输入框，按回车才发送，
            # 让用户有机会改掉听错的字，直接治“已读乱回”。
            self.entry.icursor(tk.END)
            self.entry.focus_set()
            self.show_voice_status("识别结果已填入输入框，确认无误按回车发送（有误可直接改）", 6000)
        else:
            self.send()

    def _start_live2d(self):
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live2d_app.py")
            self.live2d_proc = subprocess.Popen(
                [sys.executable, script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.append("系统", f"形象窗口未启动：{e}")

    # ---------- GPT-SoVITS 推理服务（语音输出）----------
    def _sovits_up(self):
        from urllib.parse import urlparse
        try:
            u = urlparse(CONFIG.get("sovits_url", "http://127.0.0.1:9880"))
            host = u.hostname or "127.0.0.1"
            port = u.port or 9880
            with socket.create_connection((host, port), timeout=1):
                return True
        except Exception:
            return False

    def _free_sovits_port(self):
        """释放 9880 上残留的旧 GPT-SoVITS 服务进程（老 api.py / 上次没退出的 api_v2）。

        避免小念启动时端口被旧进程占着，导致新 api_v2 拉不起来、或请求错发到老接口
        （v1 用仪玄当默认参考音、且字段名对不上 -> 400 生成失败）。
        注意：不依赖已废弃的 wmic，改用 netstat+tasklist（Win11 自带）。
        """
        from urllib.parse import urlparse
        import re
        if os.name != "nt":
            return
        port = (urlparse(CONFIG.get("sovits_url", "http://127.0.0.1:9880")).port) or 9880
        # 1) 找到监听该端口的 PID（netstat 始终可用）
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return
        pid = None
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                m = re.search(r"(\d+)\s*$", line.strip())
                if m:
                    pid = m.group(1)
                    break
        if not pid:
            return
        # 2) 仅当占用者是 python 进程时才杀（tasklist 可靠、无需 wmic），避免误伤其它程序
        try:
            tl = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, timeout=10).stdout
        except Exception:
            tl = ""
        if "python" not in tl.lower():
            return
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True, timeout=10)
            time.sleep(1)  # 等系统释放端口，避免新服务立刻 bind 失败
            self.append("系统", "已清理 9880 端口上的旧语音服务，重启为八重神子参考音。")
        except Exception:
            pass

    def _start_sovits(self):
        if getattr(self, "_sovits_starting", False):
            return
        # 先清掉 9880 上残留的旧服务（老 api.py / 上次没退出的 api_v2），保证这次用 api_v2 + 八重神子
        self._free_sovits_port()
        home = CONFIG.get("sovits_home", "")
        if not home or not os.path.isdir(home):
            self.show_voice_status("未配置 SOVITS_HOME，无法自动启动 GPT-SoVITS", 6000)
            return
        py = os.path.join(home, "runtime", "python.exe")
        if not os.path.exists(py):
            # 回退：优先用 .env 的 SOVITS_PYTHON（通常是已配好 cu128 torch 的独立 venv，
            # 修 RTX50 系 sm_120 的 "no kernel image" 崩溃），再回退本机 D:\sovits_env。
            alt = CONFIG.get("sovits_python", "").strip()
            if alt and os.path.exists(alt):
                py = alt
            elif os.path.exists(r"D:\sovits_env\python.exe"):
                py = r"D:\sovits_env\python.exe"
            else:
                self.show_voice_status("未找到 GPT-SoVITS 的 runtime/python.exe", 6000)
                return
        ref = CONFIG.get("sovits_ref_audio", "")
        reftext = CONFIG.get("sovits_ref_text", "")
        # 使用 v2 官方接口 api_v2.py + tts_infer.yaml 的 custom 段
        # （device: cuda + gsv-v2final 底模）：音质好、噪音小，且真正用上 5070Ti。
        # 注意 api_v2 不认老 api.py 的 -d/-g/-s/-dr 参数；模型/参考音频走 yaml，
        # 参考音频也可由 voice.py 每次 POST 时按 .env 的 SOVITS_REF_AUDIO 覆盖。
        args = [py, "api_v2.py", "-a", "127.0.0.1", "-p", "9880",
                "-c", "GPT_SoVITS/configs/tts_infer.yaml"]
        env = dict(os.environ)
        env["PATH"] = os.path.join(home, "runtime") + os.pathsep + env.get("PATH", "")
        # 让 api_v2 全程用 UTF-8（Windows 默认 GBK），避免回复里带 emoji/生僻字时
        # 服务端内部 print 抛 'gbk' codec can't encode 崩溃 -> 返回 400 生成失败。
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sovits.log")
        try:
            self._sovits_starting = True
            self.sovits_proc = subprocess.Popen(
                args, cwd=home, env=env,
                stdout=open(log_path, "ab"), stderr=subprocess.STDOUT,
            )
            self.show_voice_status("正在启动 GPT-SoVITS 声音合成…（首次加载几十秒）", 6000)
        except Exception as e:
            self.show_voice_status("启动 GPT-SoVITS 失败：" + str(e), 6000)
        finally:
            self._sovits_starting = False

    def _on_close(self):
        """主窗口关闭：先结束形象/语音子进程，再销毁窗口，避免残留。"""
        try:
            if getattr(self, "screen_watcher", None) is not None:
                self.screen_watcher.stop()
        except Exception:
            pass
        for attr in ("live2d_proc", "sovits_proc"):
            proc = getattr(self, attr, None)
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.root.destroy()

    def live2d_say(self, text, name=None, start_talk=False, emotion=None):
        """向形象窗口发送「气泡 + 说话动作」。

        start_talk=True 时额外带 talk_start 标记，让前端进入实时口型模式
        （锁定当前气泡为说话气泡、口型改由 setMouth 按音频能量驱动），长句也同步。
        emotion: 可选，当前主导情绪维度名(joy/anger/sadness/calm/anxiety)，
        用于让前端按心情选对应动作/表情（动作自适应情绪）。
        """
        if not CONFIG.get("live2d_enabled"):
            return
        name = name or CONFIG["name"]
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                # 单条消息同时携带气泡文本与“说话”动作标记；接收端一次 recv 后统一处理，
                # 避免两条 sendall 在同一连接上被 TCP 粘包导致 json 解析失败（气泡/动作全丢）。
                # args 传对象：emotion 主导情绪（驱动前端按情绪选动作/表情），kind 标记说话意图。
                payload = {
                    "text": text, "name": name, "motion": True,
                    "args": {"emotion": emotion or "calm", "kind": "speaking"},
                }
                if start_talk:
                    payload["talk_start"] = True
                s.sendall(json.dumps(payload).encode("utf-8"))
        except Exception:
            pass

    def _send_mouth(self, rms):
        """把当前播放位置的音频能量(0~1)发给形象窗口，驱动实时口型。"""
        if not CONFIG.get("live2d_enabled"):
            return
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                s.sendall(json.dumps({"mouth": float(rms)}).encode("utf-8"))
        except Exception:
            pass

    def _stop_talk(self):
        """音频播完：通知前端口型归零并淡出气泡。"""
        if not CONFIG.get("live2d_enabled"):
            return
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                s.sendall(json.dumps({"talk_stop": True}).encode("utf-8"))
        except Exception:
            pass

    def _send_live2d_action(self, act):
        """向形象窗口发送一个动作指令（jump / turn 等）。"""
        if not act or not CONFIG.get("live2d_enabled"):
            return
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                s.sendall(json.dumps({"action": act}).encode("utf-8"))
        except Exception:
            pass

    def _detect_action(self, text):
        """识别用户话语里的动作意图：跳 / 转身。"""
        t = text or ""
        if any(k in t for k in ("跳", "蹦", "跳一下", "蹦一下", "跳起来")):
            return "jump"
        if any(k in t for k in ("转身", "转个身", "转过去", "转一圈", "转个圈")):
            return "turn"
        return None

    # ---------- 回复队列：用户连续输入时按先后依次输出 ----------
    def _init_reply_queue(self):
        """初始化回复队列与主动关心计时所需的全部状态。"""
        self.user_queue = []
        self.user_q_lock = threading.Lock()
        self.user_q_running = False
        self.busy_with_user = False
        self._speak_lock = threading.Lock()
        self._care_question = ""
        self._care_timer = None
        self._care_retry = None
        self._last_user_activity = time.time()   # 最近一次“用户动作”（输入/切窗）时间
        self._app_chat_exe = None                # 已就“连续使用”搭过话的软件（防同软件重复）
        self._last_idle_care = 0.0               # 上次空闲关心时间（限频，避免刷屏）

    def _enqueue_user(self, text):
        """把一条用户发言放入回复队列，并启动处理 worker（若未运行）。"""
        with self.user_q_lock:
            self.user_queue.append(text)
        self._last_user_activity = time.time()   # 用户发消息 = 一次动作
        self._pump_user_queue()

    def _pump_user_queue(self):
        if self.user_q_running:
            return
        self.user_q_running = True
        threading.Thread(target=self._user_queue_worker, daemon=True).start()

    def _user_queue_worker(self):
        """串行处理用户发言：每次只处理一条，播完再处理下一条。"""
        while True:
            with self.user_q_lock:
                if not self.user_queue:
                    self.user_q_running = False
                    return
                text = self.user_queue.pop(0)
            # 标记“正在回复用户”，期间看屏/主动关心内容一律让位
            self.busy_with_user = True
            try:
                self._schedule_care(text)   # 提问后 6-10 分钟自动触发一次“主动关心”
                self._reply_one(text)
            except Exception as e:
                self.root.after(0, lambda e=e: self.append("出错了", str(e)))
            finally:
                self.busy_with_user = False

    def _user_active(self):
        """用户是否正在被回复，或还有未处理的提问（此时主动内容应让位）。"""
        return self.busy_with_user or bool(self.user_queue)

    # ---------- 主动关心：提问后 6-10 分钟，关联上下文触发 ----------
    def _schedule_care(self, question):
        """在用户提问后 6-10 分钟，自动触发一次与提问内容相关的“主动关心”。

        若期间又有新提问，会重置计时（始终关联最新一次提问）。
        """
        import random
        self._care_question = question
        # 取消上一次计时（含重试计时），避免叠加/串台
        for _t in (self._care_timer, self._care_retry):
            if _t is not None:
                try:
                    _t.cancel()
                except Exception:
                    pass
        self._care_timer = None
        self._care_retry = None
        delay = random.uniform(6 * 60, 10 * 60)   # 6~10 分钟
        self._care_timer = threading.Timer(delay, self._fire_care)
        self._care_timer.daemon = True
        self._care_timer.start()

    def _fire_care(self):
        """触发主动关心。若此刻用户正在被回复，则让位（稍后重试一次），优先保证用户提问。"""
        if self._user_active():
            # 用户正忙：1 分钟后再试一次，不丢关心（不碰 _care_timer，避免误清新计时）
            if self._care_retry is None or not self._care_retry.is_alive():
                self._care_retry = threading.Timer(60, self._fire_care)
                self._care_retry.daemon = True
                self._care_retry.start()
            return
        q = self._care_question
        try:
            msg = self.assistant.care_message(q)
            if msg:
                self.root.after(0, self._show_reply, CONFIG["name"], "（关心）" + msg, True)
        except Exception:
            pass

    # ---------- 并行条件循环：空闲关心 / 软件搭话 ----------
    def start_proactive(self):
        """并行条件循环：根据屏幕信息判断用户状态，主动关心 / 搭话。

        条件1（空闲关心）：用户超过半小时没有任何动作——既没对小念说话，也没切换窗口
            （通过屏幕监控判断）——则基于“当前屏幕内容 + 之前对话”生成关联性关心。
        条件2（软件搭话）：用户连续使用同一款软件超过 10 分钟，则解析屏幕内容主动搭话。

        两条都受优先级规则约束：当用户正在被回复 / 还有待回复的提问时，主动内容让位。
        """
        def loop():
            while True:
                time.sleep(30)   # 每 30 秒轮询一次屏幕状态
                try:
                    self._proactive_tick()
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True).start()

    def _proactive_tick(self):
        """每轮轮询：判断是否满足“空闲关心”或“软件搭话”条件。"""
        watcher = getattr(self, "screen_watcher", None)
        now = time.time()

        # “用户动作”时间 = 最近一次对小念的输入 与 最近一次窗口切换 的较晚者
        last_switch = watcher.current_state()["last_switch"] if watcher else now
        last_action = max(self._last_user_activity, last_switch)
        idle_for = now - last_action

        # —— 条件2：连续使用某款软件 > 10 分钟 → 解析屏幕搭话（每个软件一次）——
        if watcher is not None:
            st = watcher.current_state()
            if st["exe"] and st["dwell_seconds"] >= 10 * 60 and st["exe"] != self._app_chat_exe:
                if self._user_active():
                    return   # 用户正忙，本次不搭话；保留标记以便稍后重试
                self._app_chat_exe = st["exe"]
                self._proactive_app_chat(st)
                return       # 本次循环只处理一个触发，避免并发抢话

        # —— 条件1：超过半小时没有任何动作 → 基于屏幕+历史的关心 ——
        if idle_for >= 30 * 60 and (now - self._last_idle_care) >= 30 * 60:
            self._last_idle_care = now
            self._last_user_activity = now   # 重置空闲计时，避免一直刷
            self._proactive_idle_care(watcher)

    def _screen_context(self, app_name=""):
        """拿到当前屏幕的“客观描述”：视觉可用就截屏看懂，否则用窗口标题/程序名。"""
        ctx = ""
        try:
            import vision
            if vision.is_available():
                desc = vision.describe_screen()
                if desc:
                    ctx = desc
        except Exception:
            ctx = ""
        if not ctx:
            watcher = getattr(self, "screen_watcher", None)
            if watcher is not None:
                st = watcher.current_state()
                t = st.get("title") or ""
                a = st.get("app") or app_name or "某个程序"
                ctx = f"「{a}」" + (f"（窗口标题：{t}）" if t and t != a else "")
            elif app_name:
                ctx = f"「{app_name}」"
        return ctx

    def _proactive_app_chat(self, st):
        """条件2：用户连续使用某软件 >10 分钟，解析屏幕内容主动搭话。"""
        if self._user_active():
            return
        app_name = st.get("app") or "某个程序"
        screen = self._screen_context(app_name)
        try:
            msg = self.assistant.app_chat_message(screen, app_name)
        except Exception:
            return
        if msg:
            self.root.after(0, self._show_reply, CONFIG["name"], "（搭话）" + msg, True)

    def _proactive_idle_care(self, watcher):
        """条件1：用户超过半小时没动作，基于当前屏幕+历史对话关心。"""
        if self._user_active():
            return
        screen = self._screen_context()
        try:
            msg = self.assistant.idle_care_message(screen)
        except Exception:
            return
        if msg:
            self.root.after(0, self._show_reply, CONFIG["name"], "（关心）" + msg, True)

    def start_screen_watch(self):
        """启动屏幕活动监控：看用户在玩什么/用什么软件，适时给正反馈。"""
        self.screen_watcher = None
        if not CONFIG.get("screen_watch_enabled"):
            return
        if self.assistant is None:
            return
        try:
            from screen_watch import ScreenWatcher
        except Exception as e:
            self.append("系统", f"屏幕监控未启动：{e}")
            return

        def on_event(event):
            # 行为信号 → 自主引擎（用于习惯分析与自调参，如深夜久坐提醒更频繁）
            if self.autonomy is not None:
                try:
                    self.autonomy.record_event(event)
                except Exception:
                    pass
            # 行为信号 → 情感引擎（玩家行为也会让小念产生情绪，如深夜久坐让她略不安）
            if self.emotion is not None:
                try:
                    self.emotion.perceive(event=event, source="behavior")
                except Exception:
                    pass
            # 收到屏幕活动事件 → 让小念生成一条正反馈并说出来（复用回复管线）
            try:
                msg = self.assistant.screen_feedback(event)
                if msg:
                    self.root.after(0, self._show_reply, CONFIG["name"], "（看屏）" + msg, True)
            except Exception:
                pass

        try:
            self.screen_watcher = ScreenWatcher(
                on_event=on_event,
                interval_sec=CONFIG.get("screen_watch_interval_sec", 5),
                settle_sec=CONFIG.get("screen_watch_settle_sec", 20),
                min_gap_min=CONFIG.get("screen_watch_min_gap_min", 10),
                milestones_min=CONFIG.get("screen_watch_milestones", (30, 60, 120)),
                capture=CONFIG.get("screen_capture_enabled", False),
                data_dir=CONFIG.get("data_dir", "."),
                ignore=CONFIG.get("screen_watch_ignore", []),
                self_names=[CONFIG.get("name", "小念")],
            )
            self.screen_watcher.start()
        except Exception as e:
            self.append("系统", f"屏幕监控启动失败：{e}")

    # ---------- 自主权限：GUI 侧回调（确认弹窗 / 提示 / 参数生效）----------
    def request_confirm(self, title, message):
        """后台线程里请求用户确认（弹窗）。无 gui / 超时 → 安全拒绝。

        必须在主线程弹 messagebox，故用 after(0,...) 派发，再用 Event 等结果；
        超时（默认 180s 无应答）按“拒绝”处理，保证 fail-safe。
        """
        ev = threading.Event()
        ans = {}

        def popup():
            try:
                ans["v"] = messagebox.askyesno(title, message, parent=self.root)
            except Exception:
                ans["v"] = False
            ev.set()

        try:
            self.root.after(0, popup)
        except Exception:
            return False
        ev.wait(timeout=180)
        return bool(ans.get("v", False))

    def autonomy_toast(self, msg, ms=6000):
        """小念自主调整时的轻提示（复用语音状态条，自动消失）。"""
        self.show_voice_status(msg, ms)

    def update_screen_watch_params(self):
        """把最新 CONFIG 推给运行中的屏幕监控器，使自主调过的参数即时生效。"""
        w = getattr(self, "screen_watcher", None)
        if w is None:
            return
        try:
            w.interval = max(2, int(CONFIG.get("screen_watch_interval_sec", 5)))
            w.settle_sec = max(5, int(CONFIG.get("screen_watch_settle_sec", 20)))
            w.min_gap = max(30.0, float(CONFIG.get("screen_watch_min_gap_min", 10)) * 60.0)
            w.milestones = sorted(set(
                int(m) for m in CONFIG.get("screen_watch_milestones", (30, 60, 120)) if int(m) > 0
            ))
        except Exception:
            pass

    def on_autonomy_changed(self, key):
        """自主引擎改完参数后的回调：屏幕相关项即时推给监控器。"""
        if key is None or key.startswith("screen_watch_"):
            self.update_screen_watch_params()

    # ---------- 情感系统：GUI 侧回调（面板刷新 / 提示 / 定时分析）----------
    def on_emotion_changed(self, snapshot):
        """情感引擎状态变化后的回调：刷新控制台面板 + 可视化心情窗口。"""
        try:
            self.root.after(0, self._refresh_emotion_panel)
        except Exception:
            pass
        try:
            self.root.after(0, self._refresh_emotion_window)
        except Exception:
            pass

    def emotion_toast(self, msg, ms=6000):
        """小念性格/情绪变化的轻提示（复用系统消息，自动消失）。"""
        try:
            self.append("系统", msg)
        except Exception:
            pass

    def _refresh_emotion_panel(self):
        if getattr(self, "emotion_status", None) is None or self.emotion is None:
            return
        s = self.emotion.snapshot()
        lines = [f"性格底色：{s['personality']}　主导情绪：{s['dominant']}"]
        for k, v in s["emotion"].items():
            bar = "█" * int(v * 16)
            lines.append(f"  {k} {bar} {v:.2f}")
        try:
            self.emotion_status.config(text="\n".join(lines))
        except Exception:
            pass

    def _start_emotion_loop(self):
        """定时分析性格演变（性格变化很慢，需长期累计差值 + 稳定多次）。"""
        import threading as _th
        interval = max(300, int(CONFIG.get("emotion_analyze_min", "10")) * 60)

        def loop():
            import time as _t
            while True:
                _t.sleep(interval)
                if self.emotion is not None:
                    try:
                        self.emotion.analyze_personality()
                    except Exception:
                        pass

        _th.Thread(target=loop, daemon=True).start()

    # ---------- 可视化「心情面板」（独立窗口，可开关）----------
    def _emo_colors(self):
        """情绪维度 → 颜色（开心/生气/伤心/平静/不安）。"""
        return {
            "开心": "#ff8fb1", "生气": "#ff6b6b", "伤心": "#7aa7ff",
            "平静": "#7ee0c0", "不安": "#ffd166",
        }

    def toggle_emotion_panel(self):
        """开关可视化心情面板（控制台按钮 / 快捷键 Ctrl+Alt+E）。"""
        win = getattr(self, "emotion_win", None)
        if win is not None and win.winfo_exists():
            self._close_emotion_panel()
        else:
            self._open_emotion_panel()

    def _close_emotion_panel(self):
        win = getattr(self, "emotion_win", None)
        if win is not None and win.winfo_exists():
            try:
                win.destroy()
            except Exception:
                pass
        self.emotion_win = None

    def _open_emotion_panel(self):
        if self.emotion is None:
            return
        # 已开则聚焦，不重复创建
        if getattr(self, "emotion_win", None) and self.emotion_win.winfo_exists():
            self.emotion_win.lift()
            self.emotion_win.focus_force()
            return
        TH = getattr(self, "THEME", None)
        panel = TH["panel"] if TH else "#2a2440"
        accent = TH["accent"] if TH else "#ff8fb1"
        muted = TH["muted"] if TH else "#9a93b0"
        text = TH["text"] if TH else "#f3eefb"

        win = tk.Toplevel(self.root)
        self.emotion_win = win
        win.title(f"{CONFIG['name']} · 心情面板")
        win.geometry("380x480")
        win.configure(bg=panel)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_emotion_panel)

        # —— 滚动容器：内容超高时可滚动查看 ——
        _scroller = tk.Frame(win, bg=panel)
        _scroller.pack(fill=tk.BOTH, expand=True)
        _canvas = tk.Canvas(_scroller, bg=panel, highlightthickness=0)
        _vsb = tk.Scrollbar(_scroller, orient=tk.VERTICAL, command=_canvas.yview)
        _canvas.configure(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = tk.Frame(_canvas, bg=panel)
        _canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: _canvas.configure(scrollregion=_canvas.bbox("all")))
        _canvas.bind_all("<MouseWheel>",
                         lambda e: _canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        win.bind("<Destroy>", lambda e: _canvas.unbind_all("<MouseWheel>"))

        # —— 当前性格（可变化）——
        tk.Label(inner, text="当前性格底色", fg=muted, bg=panel,
                 font=("Microsoft YaHei", 9)).pack(anchor="w", padx=16, pady=(12, 0))
        win.trait_lbl = tk.Label(inner, text="温柔平静", fg=accent, bg=panel,
                                 font=("Microsoft YaHei", 22, "bold"))
        win.trait_lbl.pack(anchor="w", padx=16, pady=(0, 2))
        win.sub_lbl = tk.Label(inner, text="", fg=muted, bg=panel,
                               font=("Microsoft YaHei", 9), wraplength=340,
                               justify="left")
        win.sub_lbl.pack(anchor="w", padx=16, pady=(0, 6))

        # —— 情绪波动（实时条）——
        tk.Label(inner, text="情绪波动（实时）", fg=muted, bg=panel,
                 font=("Microsoft YaHei", 9)).pack(anchor="w", padx=16, pady=(4, 2))
        win.bar_canvas = tk.Canvas(inner, width=348, height=140, bg=panel,
                                   highlightthickness=0)
        win.bar_canvas.pack(padx=8)

        # —— 最近的情绪起伏（曲线）——
        tk.Label(inner, text="最近的情绪起伏", fg=muted, bg=panel,
                 font=("Microsoft YaHei", 9)).pack(anchor="w", padx=16, pady=(6, 2))
        win.spark_canvas = tk.Canvas(inner, width=348, height=150, bg=panel,
                                     highlightthickness=0)
        win.spark_canvas.pack(padx=8, pady=(0, 6))

        # —— 底部按钮（自包含，兼容有无 THEME/_btn 的副本）——
        btn_bg = TH["btn"] if TH else "#3a3358"
        btn_hot = TH["btn_hot"] if TH else "#4a4270"
        btn_row = tk.Frame(inner, bg=panel)
        btn_row.pack(pady=6)

        def _mk_btn(parent, txt, cmd):
            b = tk.Button(parent, text=txt, command=cmd, bg=btn_bg, fg=text,
                          activebackground=btn_hot, activeforeground=text,
                          relief=tk.FLAT, borderwidth=0,
                          font=("Microsoft YaHei", 9, "bold"), cursor="hand2",
                          width=11)
            return b

        _mk_btn(btn_row, "查看心情", self._emo_view_feelings).pack(side=tk.LEFT, padx=5)
        _mk_btn(btn_row, "重置情感", self._emo_reset).pack(side=tk.LEFT, padx=5)
        _mk_btn(btn_row, "关闭面板", self._close_emotion_panel).pack(side=tk.LEFT, padx=5)

        # 立即刷新一帧 + 启动采样（每 2 秒记录一次起伏）
        self.emotion_history = []
        self._refresh_emotion_window()
        self.root.after(2000, self._emo_sample)

    def _emo_view_feelings(self):
        if self.emotion is not None:
            try:
                messagebox.showinfo(f"{CONFIG['name']} 的心情",
                                    self.emotion.describe(), parent=self.root)
            except Exception:
                pass

    def _emo_reset(self):
        if self.emotion is None:
            return
        if messagebox.askyesno("重置小念的情感",
                               "确定要清空小念的情绪与性格、恢复初始「温柔平静」吗？",
                               parent=self.root):
            self.emotion.reset()
            self.emotion_history = []
            self._refresh_emotion_window()
            self._refresh_emotion_panel()

    def _emo_sample(self):
        """每 2 秒采一次情绪快照，画成起伏曲线（仅面板开着时运行）。"""
        win = getattr(self, "emotion_win", None)
        if win is None or not win.winfo_exists():
            return
        try:
            s = self.emotion.snapshot()
            self.emotion_history.append(s["emotion"])
            if len(self.emotion_history) > 120:
                self.emotion_history.pop(0)
            self._refresh_emotion_window()
        except Exception:
            pass
        self.root.after(2000, self._emo_sample)

    def _refresh_emotion_window(self):
        win = getattr(self, "emotion_win", None)
        if win is None or not win.winfo_exists():
            return
        try:
            s = self.emotion.snapshot()
            win.trait_lbl.config(text=s["personality"])
            dom, sec = s["dominant"], s["secondary"]
            sub = f"此刻主导情绪：{dom}"
            if sec:
                sub += f"　·　偶尔带着「{sec}」的底色"
            win.sub_lbl.config(text=sub)
            self._draw_emotion_bars(win.bar_canvas, s["emotion"])
            self._draw_emotion_spark(win.spark_canvas)
        except Exception:
            pass

    def _draw_emotion_bars(self, canvas, emotion):
        canvas.delete("all")
        W = canvas.winfo_width() or 348
        H = canvas.winfo_height() or 140
        colors = self._emo_colors()
        dims = list(emotion.keys())   # 开心/生气/伤心/平静/不安
        n = len(dims)
        row_h = H / n
        label_w = 40
        track_x = label_w + 6
        track_w = W - track_x - 40
        for i, dim in enumerate(dims):
            y0 = i * row_h
            cy = y0 + row_h / 2
            canvas.create_text(6, cy, text=dim, anchor="w",
                               fill="#cfc8e0", font=("Microsoft YaHei", 9))
            canvas.create_rectangle(track_x, y0 + 5, track_x + track_w, y0 + row_h - 5,
                                    fill="#1d1830", outline="")
            v = min(1.0, max(0.0, emotion[dim]))
            fw = max(2, int(track_w * v))
            canvas.create_rectangle(track_x, y0 + 5, track_x + fw, y0 + row_h - 5,
                                    fill=colors.get(dim, "#ff8fb1"), outline="")
            canvas.create_text(track_x + track_w + 4, cy, text=f"{v:.2f}",
                               anchor="w", fill="#9a93b0", font=("Microsoft YaHei", 8))

    def _draw_emotion_spark(self, canvas):
        canvas.delete("all")
        hist = getattr(self, "emotion_history", [])
        W = canvas.winfo_width() or 348
        H = canvas.winfo_height() or 150
        colors = self._emo_colors()
        pad = 8
        if len(hist) < 2:
            canvas.create_text(W / 2, H / 2,
                               text="（聊天或互动后，这里会画出情绪起伏）",
                               fill="#9a93b0", font=("Microsoft YaHei", 9))
            return
        n = len(hist)
        for dim in hist[0].keys():
            pts = []
            for i, h in enumerate(hist):
                x = pad + (W - 2 * pad) * (i / (n - 1))
                v = min(1.0, max(0.0, h.get(dim, 0)))
                y = (H - pad) - (H - 2 * pad) * v
                pts.append((x, y))
            for j in range(len(pts) - 1):
                canvas.create_line(pts[j][0], pts[j][1], pts[j + 1][0], pts[j + 1][1],
                                   fill=colors.get(dim, "#ff8fb1"), width=2)

    # ---------- 接收 live2d 窗口反向指令（输入框显隐等）----------
    def start_control_server(self):
        port = int(CONFIG.get("gui_control_port", 9744))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", port))
            srv.listen(4)
        except Exception as e:
            self.append("系统", f"控制服务未启动：{e}")
            return

        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                    data = conn.recv(65536).decode("utf-8", "ignore")
                    conn.close()
                    if not data:
                        continue
                    try:
                        msg = json.loads(data)
                    except Exception:
                        continue
                    if msg.get("toggle_input"):
                        self.root.after(0, self._toggle_input_visibility)
                except Exception:
                    continue

        threading.Thread(target=loop, daemon=True).start()

    def _toggle_input_visibility(self):
        """由 live2d 窗口的「输入框」按钮切换输入条显隐。"""
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
                try:
                    self.root.lift()
                    self.root.attributes("-topmost", CONFIG["input_topmost"])
                except Exception:
                    pass
                self.entry.focus_set()
            else:
                self.root.withdraw()
        except Exception:
            pass

    # ---------- 全局快捷键（Ctrl+Alt+V 语音 / Ctrl+Alt+G 控制台 / Ctrl+Alt+E 心情面板）----------
    def start_hotkeys(self):
        """注册全局热键（同一线程，避免重复与跨线程丢消息）：
           Ctrl+Alt+V -> 触发语音输入；Ctrl+Alt+G -> 开关“小念控制台”；
           Ctrl+Alt+E -> 开关“可视化心情面板”。
        关键修复（两个都会让热键彻底失灵，已修）：
        1) RegisterHotKey 的 hWnd 必须传 NULL(0)，不能传 HWND_MESSAGE(-3)，
           否则本机返回 1400(INVALID_WINDOW_HANDLE) 注册失败、两个键都没反应。
        2) 注册和 GetMessageW 消息泵必须在【同一个线程】里：
           RegisterHotKey 会把 WM_HOTKEY 投递到「注册它的那个线程」的消息队列，
           若在主线程注册、却在守护线程取消息，主线程被 tkinter 占着，
           守护线程永远等不到 -> 必须在这里的 loop 线程里一并注册。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            import ctypes.wintypes   # 必须显式导入子模块，否则 ctypes.wintypes.MSG 报 AttributeError
            user32 = ctypes.windll.user32
        except Exception:
            return
        MOD_CTRL, MOD_ALT, MOD_NOREPEAT = 0x0002, 0x0001, 0x4000
        VK_V, VK_G, VK_E, VK_M = 0x56, 0x47, 0x45, 0x4D
        WM_HOTKEY = 0x0312

        def loop():
            try:
                # 注册 + 消息泵同线程（hWnd 用 NULL=0，绝不用 HWND_MESSAGE）
                ok_v = user32.RegisterHotKey(0, 1, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, VK_V)
                ok_c = user32.RegisterHotKey(0, 2, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, VK_G)
                ok_e = user32.RegisterHotKey(0, 3, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, VK_E)
                ok_m = user32.RegisterHotKey(0, 4, MOD_CTRL | MOD_ALT | MOD_NOREPEAT, VK_M)
                if not ok_v or not ok_c or not ok_e or not ok_m:
                    # 注册失败大多是该组合键已被其它程序占用（错误码 1400/1401/1402）
                    try:
                        with open(os.path.join(CONFIG["data_dir"], "hotkey.log"),
                                  "a", encoding="utf-8") as f:
                            f.write(f"[hotkey] 注册失败：Ctrl+Alt+V={ok_v} Ctrl+Alt+G={ok_c} "
                                    f"Ctrl+Alt+E={ok_e} Ctrl+Alt+M={ok_m} "
                                    f"（可能被其它软件占用，需更换快捷键）\n")
                    except Exception:
                        pass
                msg = ctypes.wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == WM_HOTKEY:
                        if msg.wParam == 1:
                            self.root.after(0, self._mic_toggle)
                        elif msg.wParam == 2:
                            self.root.after(0, self.toggle_console)
                        elif msg.wParam == 3:
                            self.root.after(0, self.toggle_emotion_panel)
                        elif msg.wParam == 4:
                            self.root.after(0, self.view_memory)
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception:
                pass

        threading.Thread(target=loop, daemon=True).start()

    def toggle_console(self):
        if getattr(self, "console_win", None) and self.console_win.winfo_exists():
            self.console_win.destroy()
            self.console_win = None
            return
        self.open_console()

    def set_input_visible(self, visible):
        """直接设定输入条显隐（控制台复选框用，避免 toggle 的二义性）。"""
        try:
            if visible:
                self.root.deiconify()
                self.root.lift()
                self.root.attributes("-topmost", CONFIG["input_topmost"])
                self.entry.focus_set()
            else:
                self.root.withdraw()
        except Exception:
            pass

    def _send_live2d_msg(self, msg):
        """向形象窗口发送一条控制指令（大小/气泡/复位等）。"""
        if not CONFIG.get("live2d_enabled"):
            return
        try:
            with socket.create_connection(("127.0.0.1", CONFIG["live2d_port"]), timeout=1) as s:
                s.sendall(json.dumps(msg).encode("utf-8"))
        except Exception:
            pass

    def _send_live2d_scale(self, v):
        self._send_live2d_msg({"scale": float(v)})

    def _send_live2d_bubble(self, on):
        self._send_live2d_msg({"bubble": bool(on)})

    def _send_live2d_reset(self):
        self._send_live2d_msg({"reset": True})

    def open_console(self):
        """整合控制台：一个窗口控制小念的所有参数。整体放进可滚动区域，
        窗口放大且可拖拽缩放，避免功能显示不全。"""
        win = tk.Toplevel(self.root)
        self.console_win = win
        win.title(f"{CONFIG['name']} · 控制台")
        win.attributes("-topmost", True)
        win.resizable(True, True)
        try:
            x = self.root.winfo_x() + 20
            y = self.root.winfo_y() - 20
            if y < 0:
                y = 0
            win.geometry(f"320x620+{x}+{y}")
            win.minsize(280, 360)
        except Exception:
            pass

        # ---- 滚动容器：Canvas + 右侧滚动条 + 内层 frame ----
        canvas = tk.Canvas(win, highlightthickness=0)
        scroll = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas)
        canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        # 滚轮滚动（窗口关闭时解绑，避免影响其它窗口）
        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        def _on_close():
            canvas.unbind_all("<MouseWheel>")
            win.destroy()
            setattr(self, "console_win", None)
        win.protocol("WM_DELETE_WINDOW", _on_close)

        # ---- 以下所有控件放进 body（可滚动）----
        tk.Label(body, text="快捷键 Ctrl+Alt+G 控制台 · Ctrl+Alt+E 心情面板 · Ctrl+Alt+V 语音 · Ctrl+Alt+M 记忆",
                 font=("Microsoft YaHei", 9), fg="#8a8a8a").pack(pady=(8, 2))

        # ---- 模型大小 ----
        tk.Label(body, text="模型大小", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        scale_var = tk.DoubleVar(value=1.0)
        tk.Scale(body, from_=0.3, to=2.2, resolution=0.01, orient=tk.HORIZONTAL,
                 variable=scale_var, length=270,
                 command=lambda v: self._send_live2d_scale(float(v))).pack(padx=12)
        tk.Button(body, text="复位模型大小/位置", width=26,
                  command=self._send_live2d_reset).pack(pady=2)

        # ---- 显示开关 ----
        input_var = tk.BooleanVar(value=(self.root.state() != "withdrawn"))
        tk.Checkbutton(body, text="显示输入框", variable=input_var,
                       command=lambda: self.set_input_visible(input_var.get())
                       ).pack(anchor="w", padx=14, pady=(4, 0))
        bubble_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text="显示对话气泡", variable=bubble_var,
                       command=lambda: self._send_live2d_bubble(bubble_var.get())
                       ).pack(anchor="w", padx=14)

        # ---- 输入条透明度 / 语音音量 / 降噪 ----
        tk.Label(body, text="输入框透明度", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        alpha_var = tk.DoubleVar(value=self.style["alpha"])
        tk.Scale(body, from_=0.3, to=1.0, resolution=0.01, orient=tk.HORIZONTAL,
                 variable=alpha_var, length=270,
                 command=lambda v: self._set_alpha(float(v))).pack(padx=12)
        tk.Label(body, text="语音音量", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        vol_var = tk.DoubleVar(value=self.style["volume"])
        tk.Scale(body, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL,
                 variable=vol_var, length=270,
                 command=lambda v: self._set_volume(float(v))).pack(padx=12)
        denoise_var = tk.BooleanVar(value=bool(self.tts.if_sr))
        tk.Checkbutton(body, text="语音降噪（更干净/略慢）", variable=denoise_var,
                       command=lambda: self._set_denoise(denoise_var.get())
                       ).pack(anchor="w", padx=14, pady=2)

        # ---- 对话模型 ----
        tk.Label(body, text="对话模型", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        mst = self._load_model_state()
        model_var = tk.StringVar(value=mst["current"])
        model_menu = tk.OptionMenu(body, model_var, *mst["models"],
                                   command=lambda v: self._on_model_change(v, mst["models"]))
        model_menu.config(width=32)
        model_menu.pack(padx=12, fill=tk.X)

        # ---- API 设置（运行时更换服务商 / 密钥 / 模型）----
        self._build_api_section(body)

        # ---- 视觉 API 设置（GLM-4V-Flash 等看屏模型）----
        self._build_vision_api_section(body)

        # ---- 语音音色 ----
        tk.Label(body, text="语音音色", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        vst = self._load_voice_state()
        voice_names = list(vst["presets"].keys()) or ["（无可用音色）"]
        voice_var = tk.StringVar(value=vst["current"])
        voice_menu = tk.OptionMenu(body, voice_var, *voice_names,
                                   command=lambda v: self._on_voice_change(v, vst["presets"]))
        voice_menu.config(width=32)
        voice_menu.pack(padx=12, fill=tk.X)

        # ---- Live2D 形象 ----
        tk.Label(body, text="Live2D 形象", anchor="w").pack(fill=tk.X, padx=14, pady=(6, 0))
        ld_models = self._discover_live2d_models()
        ld_map = {n: r for n, r in ld_models}
        ld_names = list(ld_map.keys()) or ["（无可用模型）"]
        cur_rel = CONFIG.get("live2d_model", "")
        ld_sel = next((n for n, r in ld_models if r == cur_rel), ld_names[0])
        ld_var = tk.StringVar(value=ld_sel)
        ld_menu = tk.OptionMenu(body, ld_var, *ld_names,
                                command=lambda v: self._on_live2d_model_change(v, ld_map))
        ld_menu.config(width=32)
        ld_menu.pack(padx=12, fill=tk.X)

        # ---- 小念的自主权限（受约束自调参）----
        tk.Label(body, text="小念的自主权限", anchor="w").pack(fill=tk.X, padx=14, pady=(10, 0))
        a_status = tk.Label(
            body,
            text=("已开启（在白名单内自调参，大改动会先问你）"
                  if (self.autonomy and self.autonomy.enabled) else "已关闭（改动全由你定）"),
            fg="#2e7d32" if (self.autonomy and self.autonomy.enabled) else "#c62828",
            font=("Microsoft YaHei", 9), wraplength=280, justify="left",
        )
        a_status.pack(anchor="w", padx=14)

        a_row = tk.Frame(body)
        a_row.pack(pady=(4, 2))

        def _review_changes():
            if self.autonomy is None:
                return
            txt = self.autonomy.review()
            messagebox.showinfo(f"{CONFIG['name']} 的自主改动", txt, parent=self.root)

        def _reset_changes():
            if self.autonomy is None:
                return
            if messagebox.askyesno("撤销小念的改动",
                                   "确定要撤销小念所有自主调整、恢复你的基线设置吗？",
                                   parent=self.root):
                self.autonomy.reset_all()
                a_status.config(text="已关闭（改动全由你定）", fg="#c62828")

        def _toggle_autonomy():
            if self.autonomy is None:
                return
            on = not self.autonomy.enabled
            self.autonomy.set_mode(on)
            a_status.config(
                text=("已开启（在白名单内自调参，大改动会先问你）" if on
                      else "已关闭（改动全由你定）"),
                fg="#2e7d32" if on else "#c62828",
            )

        tk.Button(a_row, text="查看改动", width=12, command=_review_changes).pack(side=tk.LEFT, padx=6)
        tk.Button(a_row, text="撤销全部", width=12, command=_reset_changes).pack(side=tk.LEFT, padx=6)
        tk.Button(body, text="开关自主权限", width=26, command=_toggle_autonomy).pack(pady=(2, 6))
        tk.Label(body,
                 text="小念只在白名单内改配置文件，绝不碰系统/代码/你的文件；"
                      "作息类大调整会弹窗问你。",
                 fg="#8a8a8a", font=("Microsoft YaHei", 8), wraplength=280,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))

        # ---- 小念的性格与情绪 ----
        tk.Label(body, text="小念的性格与情绪", fg="#ffd9e8",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", padx=14, pady=(8, 2))
        tk.Label(body,
                 text="情绪实时波动 · 性格缓慢演变",
                 fg="#8a8a8a", font=("Microsoft YaHei", 8), wraplength=280,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))
        self.emotion_status = tk.Label(
            body, text="", fg="#ffd9e8",
            font=("Microsoft YaHei", 9), wraplength=280, justify="left",
        )
        self.emotion_status.pack(anchor="w", padx=14)
        self._refresh_emotion_panel()

        def _view_feelings():
            if self.emotion is None:
                return
            try:
                messagebox.showinfo(f"{CONFIG['name']} 的心情", self.emotion.describe(), parent=self.root)
            except Exception:
                pass

        def _reset_feelings():
            if self.emotion is None:
                return
            if messagebox.askyesno("重置小念的情感",
                                   "确定要清空小念的情绪与性格、恢复初始「温柔平静」吗？",
                                   parent=self.root):
                self.emotion.reset()
                self._refresh_emotion_panel()

        def _toggle_emotion():
            if self.emotion is None:
                return
            on = not self.emotion.enabled
            self.emotion.set_mode(on)
            self._refresh_emotion_panel()

        e_row = tk.Frame(body)
        e_row.pack(pady=(6, 2))
        tk.Button(e_row, text="查看心情", width=12, command=_view_feelings).pack(side=tk.LEFT, padx=6)
        tk.Button(e_row, text="重置情感", width=12, command=_reset_feelings).pack(side=tk.LEFT, padx=6)
        tk.Button(e_row, text="心情面板", width=12, command=self.toggle_emotion_panel).pack(side=tk.LEFT, padx=6)
        tk.Button(body, text="开关情感系统", width=28, command=_toggle_emotion).pack(pady=(4, 6))
        tk.Label(body,
                 text="情绪随你的聊天与行为实时波动；性格变化很慢，需情绪长期积累到足够大的差值才会改变。"
                      "无论情绪如何，她让你生活更好的初心不变。",
                 fg="#8a8a8a", font=("Microsoft YaHei", 8), wraplength=280,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))

        # ---- 记忆与睡眠（性能自愈：遍历耗时逼近/超过阈值自动睡眠+压缩整合）----
        tk.Label(body, text="记忆与睡眠（性能自愈）", fg="#ffd9e8",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", padx=14, pady=(8, 2))
        tk.Label(body,
                 text="每轮遍历记忆图耗时逼近 500ms 小念会犯困想睡；超过则强制睡眠、"
                      "压缩整合记忆（弱词并入强词）。你也可以手动让她睡眠整理。",
                 fg="#8a8a8a", font=("Microsoft YaHei", 8), wraplength=280,
                 justify="left").pack(anchor="w", padx=14, pady=(0, 4))
        mind_status = tk.Label(body, text="", fg="#8a8a8a", font=("Microsoft YaHei", 8),
                               wraplength=280, justify="left")
        mind_status.pack(anchor="w", padx=14)

        def _refresh_mind_status():
            if self.assistant is None or getattr(self.assistant, "mind", None) is None:
                mind_status.config(text="意识层未启用")
                return
            st = self.assistant.mind_screening_status()
            mind_status.config(
                text=f"节点数={st.get('nodes')}  遍历≈{st.get('traverse_ms')}ms  "
                     f"睡眠态={st.get('sleep_state')}"
                     + ("  ⚠ 需你筛选存储" if st.get("screening_needed") else ""))

        def _on_sleep_done(rep):
            if "error" in rep:
                messagebox.showinfo("记忆整理", "意识层未启用或出错：" + str(rep.get("error")),
                                    parent=self.root)
                return
            msg = (f"记忆整理完成（第 {rep.get('sleep_count')} 次睡眠）\n"
                   f"节点 {rep.get('before_nodes')} → {rep.get('after_nodes')}\n"
                   f"遍历耗时 {rep.get('before_ms')}ms → {rep.get('after_ms')}ms")
            if rep.get("screening_needed"):
                _open_screening(msg)
            else:
                messagebox.showinfo("记忆整理 / 强制睡眠", msg, parent=self.root)
            _refresh_mind_status()

        def _do_sleep():
            if self.assistant is None:
                return
            mind_status.config(text="整理中…")
            threading.Thread(target=lambda: self.root.after(0, _on_sleep_done,
                                                           self.assistant.mind_force_sleep()),
                             daemon=True).start()

        def _open_screening(prev_msg):
            dlg = tk.Toplevel(self.root)
            dlg.title("记忆太多 · 请你决定怎么处理")
            dlg.transient(self.root)
            tk.Label(dlg, text=prev_msg + "\n\n压缩后遍历仍偏高，需要你来选择：",
                     wraplength=320, justify="left", font=("Microsoft YaHei", 10)).pack(padx=12, pady=10)

            def _backup():
                p = self.assistant.mind_backup()
                dlg.destroy()
                messagebox.showinfo("已备份", f"词库已备份另存：\n{p}", parent=self.root)
                _refresh_mind_status()

            def _delete():
                if messagebox.askyesno("确认清空", "确定要清空整个词库、从头再来吗？此操作不可撤销。",
                                       parent=dlg):
                    self.assistant.mind_delete()
                    dlg.destroy()
                    messagebox.showinfo("已清空", "词库已清空，小念的记忆从头开始。", parent=self.root)
                    _refresh_mind_status()

            def _filtered():
                n = simpledialog.askinteger("筛选保留", "保留强度最高的前 N 个词（其余删除并另存）：",
                                            initialvalue=300, minvalue=1, maxvalue=1000000, parent=dlg)
                if n is None:
                    return
                p = self.assistant.mind_export_filtered(set(), top_n=n)
                dlg.destroy()
                messagebox.showinfo("已筛选另存", f"已保留强度前 {n} 的词并另存：\n{p}", parent=self.root)
                _refresh_mind_status()

            r = tk.Frame(dlg)
            r.pack(pady=8)
            tk.Button(r, text="① 备份另存", width=12, command=_backup).pack(side=tk.LEFT, padx=6)
            tk.Button(r, text="② 清空重来", width=12, command=_delete).pack(side=tk.LEFT, padx=6)
            tk.Button(r, text="③ 筛选保留", width=12, command=_filtered).pack(side=tk.LEFT, padx=6)
            tk.Button(dlg, text="稍后再说", command=dlg.destroy).pack(pady=(2, 8))

        mind_row = tk.Frame(body)
        mind_row.pack(pady=(4, 2))
        tk.Button(mind_row, text="💤 强制睡眠/记忆整理", width=18, command=_do_sleep).pack(side=tk.LEFT, padx=6)
        tk.Button(mind_row, text="刷新状态", width=10, command=_refresh_mind_status).pack(side=tk.LEFT, padx=6)
        _refresh_mind_status()

        # ---- 动作 ----
        row = tk.Frame(body)
        row.pack(pady=(8, 6))
        tk.Button(row, text="跳一下", width=11,
                  command=lambda: self._send_live2d_action("jump")).pack(side=tk.LEFT, padx=8)
        tk.Button(row, text="转个身", width=11,
                  command=lambda: self._send_live2d_action("turn")).pack(side=tk.LEFT, padx=8)

    def notify(self, msg):
        win = tk.Toplevel(self.root)
        win.title(CONFIG["name"])
        win.attributes("-topmost", True)
        win.geometry("320x130")
        tk.Label(win, text=f"{CONFIG['name']} 想你啦 💕", font=("Microsoft YaHei", 12, "bold")).pack(pady=6)
        tk.Label(win, text=msg, wraplength=290, font=("Microsoft YaHei", 10)).pack(pady=6)
        tk.Button(win, text="好的", command=win.destroy).pack(pady=4)
        win.after(8000, win.destroy)
