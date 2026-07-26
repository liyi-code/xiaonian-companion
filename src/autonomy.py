"""小念的「受约束自主权限」引擎（第二阶段：自我管理 / 自调参）。

设计边界（严格对应三条约束）：
1) 小念只能在 config.AUTONOMY_WHITELIST 白名单内的配置项上微调；
   任何白名单外的 key 一律拒绝 —— 系统设置 / 源码 / 用户文件在结构上就改不到。
2) 改动只写入 data/autonomy_overrides.json 这一个配置文件；
   每次写入前先备份（.bak），并写审计日志（audit log）。
3) 「作息/设备类大调整」按白名单 confirm 规则弹窗请你确认才生效；
   小调整自动应用，但同样留日志 + 气泡提示，你随时可在控制台“撤销全部”。

健康护栏（约束3）：所有自主调整都围绕「让玩家生活越来越好」——
- 深夜久坐 → 只往“更频繁提醒休息/喝水”方向调，并加强安抚话术（劝导而非迎合）；
- 常丢文件 → 打开/调高文件自动备份旋钮（实际备份动作仍由你触发，不擅自动你文件）；
- 打游戏心态崩 → 增强鼓励话术与鼓励动画；
- 绝不主动提议“让你更能熬夜/更能肝”的任何改动。
"""

import os
import json
import time
import threading
from datetime import datetime

from config import CONFIG, AUTONOMY_WHITELIST, _autonomy_coerce

# 模块级单例：工具层(assistant/tools)通过它访问当前引擎实例
INSTANCE = None


class Autonomy:
    def __init__(self, gui=None):
        global INSTANCE
        self.gui = gui
        INSTANCE = self

        self.enabled = bool(CONFIG.get("autonomy_enabled", True))
        self.confirm_major = bool(CONFIG.get("autonomy_confirm_major", True))
        self.data_dir = CONFIG["data_dir"]
        self.overrides_path = CONFIG.get(
            "_autonomy_overrides_path",
            os.path.join(self.data_dir, "autonomy_overrides.json"),
        )
        self.audit_path = os.path.join(self.data_dir, "autonomy_audit.jsonl")
        self.log_path = os.path.join(self.data_dir, "autonomy.log")

        self._lock = threading.Lock()
        # 行为信号池：屏幕活动 + 聊天里识别到的习惯信号（滚动保留最近 200 条）
        self.signals = []
        # 同一 key 的提案冷却，避免每个分析周期都弹窗/打扰
        self._cool = {}
        self._stop = threading.Event()

    # ----------------------------------------------------------------- #
    # 生命周期
    # ----------------------------------------------------------------- #
    def start(self):
        if not self.enabled:
            self._log("自主权限已关闭（autonomy_enabled=false），不启动分析线程。")
            return
        self._stop.clear()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        interval = max(60, int(CONFIG.get("autonomy_analyze_min", 5)) * 60)
        while not self._stop.is_set():
            self._stop.wait(interval)
            if self.enabled:
                try:
                    self.analyze()
                except Exception as e:  # 自主分析失败绝不能拖垮主程序
                    self._log(f"analyze 出错（已忽略）：{e}")

    def set_mode(self, on):
        """用户随时可开/关小念的自主调整（保留最终主动权）。"""
        self.enabled = bool(on)
        self._log(f"自主权限模式被切换为：{'开' if on else '关'}。")
        return self.enabled

    # ----------------------------------------------------------------- #
    # 行为观测：屏幕活动 + 聊天信号
    # ----------------------------------------------------------------- #
    def record_event(self, event):
        """屏幕监控事件进来（昼/夜、时长、程序）。"""
        try:
            hour = datetime.now().hour
            self.signals.append({
                "cat": "dwell", "ts": time.time(), "hour": hour,
                "app": event.get("app"), "minutes": int(event.get("minutes") or 0),
                "kind": event.get("kind"),
            })
            self._trim()
        except Exception:
            pass

    def record_signal(self, cat, detail=""):
        """聊天里识别到的习惯信号：lost_file / low_mood_gaming / stay_up_intent 等。"""
        try:
            self.signals.append({
                "cat": cat, "ts": time.time(),
                "hour": datetime.now().hour, "detail": str(detail)[:200],
            })
            self._trim()
        except Exception:
            pass

    def _trim(self):
        if len(self.signals) > 200:
            self.signals = self.signals[-200:]

    # ----------------------------------------------------------------- #
    # 规则分析：把观察到的习惯变成「调整提案」
    # 所有提案都往“让生活更好 / 更健康”方向，绝不迎合有害行为。
    # ----------------------------------------------------------------- #
    def analyze(self):
        with self._lock:
            sigs = list(self.signals)
        recent = sigs[-60:]

        # 规则A：深夜（23点后或6点前）长时间用电脑 → 更频繁提醒休息 + 话术调暖
        late_long = any(
            s.get("hour") is not None and (s["hour"] >= 23 or s["hour"] < 6)
            and s.get("minutes", 0) >= 60 for s in recent if s.get("cat") == "dwell"
        )
        late_cnt = sum(
            1 for s in recent if s.get("cat") == "dwell"
            and s.get("hour") is not None and (s["hour"] >= 23 or s["hour"] < 6)
        )
        if late_long or late_cnt >= 3:
            self.propose(
                "screen_watch_min_gap_min", 5,
                "你最近常在深夜长时间用电脑，我想把休息/喝水的提醒调得更频繁一点，多心疼你。",
                category="健康", cooldown=3600,
            )
            self.propose(
                "comfort_bias", 0.4,
                "深夜久坐容易累，把我的安抚和鼓励话术调暖一点，多陪陪你。",
                category="话术", cooldown=3600,
            )

        # 规则B：常丢文件 → 打开/调高文件自动备份旋钮（实际备份仍由你触发）
        lost = [s for s in recent if s.get("cat") == "lost_file"]
        if len(lost) >= 2:
            self.propose(
                "file_backup_enabled", True,
                "你好几次说文件弄丢了，我把自动备份帮你打开，少让你心疼。",
                category="备份", cooldown=7200,
            )
            self.propose(
                "file_backup_interval_min", 15,
                "顺便把备份频率调高一点，更稳当。",
                category="备份", cooldown=7200,
            )

        # 规则C：打游戏心态崩/上头 → 加强鼓励话术 + 鼓励动画
        low = [s for s in recent if s.get("cat") == "low_mood_gaming"]
        if len(low) >= 2:
            self.propose(
                "comfort_bias", 0.6,
                "你打游戏好像有点上头/不开心，我把鼓励话术和陪伴感加强一点。",
                category="话术", cooldown=7200,
            )
            self.propose(
                "encourage_motion_enabled", True,
                "受挫时多给你加油打气的小动作，让你开心点。",
                category="动画", cooldown=7200,
            )

    # ----------------------------------------------------------------- #
    # 提案 / 应用（核心护栏都在这里）
    # ----------------------------------------------------------------- #
    def propose(self, key, value, reason, category="常规", cooldown=0, interactive=False):
        """提出一次配置调整。返回给用户/小念看的结果字符串。

        interactive=True 表示是用户主动让小念调（如聊天里要求），跳过“未开启”限制与冷却。
        """
        if not interactive and not self.enabled:
            return "（自主调整当前已关闭，我先不擅自动你的设置～）"
        if key not in AUTONOMY_WHITELIST:
            # 护栏1：白名单之外一律拒绝
            return ("这个设置不在我能安全调整的名单里。我只改白名单内的配置文件项，"
                    "系统设置、源码、你的文件我都不会碰。")

        meta = AUTONOMY_WHITELIST[key]
        new_val = _autonomy_coerce(meta, value)
        if new_val is None:
            return "想调的值不合法，我没法应用。"

        old_val = CONFIG.get(key)
        if self._same(meta, old_val, new_val):
            return f"「{meta['label']}」现在就是这个样子啦，不用改～"

        # 冷却：同一 key 短时间内不重复提案（避免刷屏）
        if cooldown and not interactive:
            now = time.time()
            if now - self._cool.get(key, 0) < cooldown:
                return ""
            self._cool[key] = now

        # 是否需弹窗确认
        need_confirm = self.confirm_major and self._needs_confirm(meta, old_val, new_val)
        if need_confirm:
            ok = self._ask_confirm(meta, old_val, new_val, reason, category)
            if not ok:
                self._audit(key, old_val, new_val, reason, confirmed=False, applied=False,
                            category=category, note="用户拒绝")
                return f"「{meta['label']}」你想让我从 {old_val} 改成 {new_val}，你拒绝了，那我就不改啦～"

        self._apply(key, new_val, reason, confirmed=need_confirm, category=category)
        return (f"已经帮你把「{meta['label']}」从 {old_val} 改成 {new_val} 啦"
                f"（只动了配置文件，不影响系统）。")

    def _apply(self, key, value, reason, confirmed, category):
        meta = AUTONOMY_WHITELIST[key]
        # 1) 备份当前覆盖层
        self._backup_overrides()
        # 2) 写入覆盖层 json（只存白名单项）
        try:
            applied = dict(CONFIG.get("_autonomy_overrides", {}))
            applied[key] = value
            with open(self.overrides_path, "w", encoding="utf-8") as f:
                json.dump(applied, f, ensure_ascii=False, indent=2)
            CONFIG["_autonomy_overrides"] = applied
        except Exception as e:
            self._log(f"写覆盖层失败：{e}")
        # 3) 立即生效（CONFIG 是全局 dict，运行期读取即用）
        CONFIG[key] = value
        # 4) 若改动的是屏幕监控类，推给运行中的监控器
        if self.gui is not None:
            try:
                self.gui.on_autonomy_changed(key)
            except Exception:
                pass
            try:
                self.gui.autonomy_toast(
                    f"🤖 我自主调整了：{meta['label']} → {value}" + ("" if confirmed else "（已记录）"))
            except Exception:
                pass
        # 5) 审计 + 日志
        self._audit(key, CONFIG.get(key), value, reason, confirmed=confirmed, applied=True,
                    category=category)

    # ----------------------------------------------------------------- #
    # 透明与撤销
    # ----------------------------------------------------------------- #
    def review(self):
        """给用户/小念看：当前自主改了什么 + 最近审计。"""
        applied = CONFIG.get("_autonomy_overrides", {})
        if not applied:
            return "我目前没有擅自改过任何设置哦，都是你定好的基线～"
        lines = ["【我已经自主调整过的设置】"]
        for k, v in applied.items():
            meta = AUTONOMY_WHITELIST.get(k)
            lines.append(f"- {meta['label'] if meta else k}: {v}")
        lines.append("\n【最近的操作记录】")
        try:
            rows = []
            with open(self.audit_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            rows = rows[-8:]
            for r in rows:
                ts = r.get("ts", "")
                try:
                    ts = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                except Exception:
                    pass
                act = "已生效" if r.get("applied") else ("被拒" if not r.get("applied") else "?")
                lines.append(f"- {ts} {r.get('category','')} {r.get('label','')} "
                             f"{r.get('old')}→{r.get('new')} [{act}]")
        except Exception:
            pass
        return "\n".join(lines)

    def reset_all(self):
        """撤销小念的全部自主改动，恢复 .env 基线（用户最终主动权）。"""
        try:
            if os.path.exists(self.overrides_path):
                self._backup_overrides()
                os.remove(self.overrides_path)
        except Exception:
            pass
        CONFIG["_autonomy_overrides"] = {}
        for k, meta in AUTONOMY_WHITELIST.items():
            CONFIG[k] = meta["default"]
        if self.gui is not None:
            try:
                self.gui.on_autonomy_changed(None)
                self.gui.autonomy_toast("🤖 已撤销小念的所有自主改动，恢复你的基线设置")
            except Exception:
                pass
        self._log("用户撤销了小念的全部自主改动。")

    # ----------------------------------------------------------------- #
    # 内部工具
    # ----------------------------------------------------------------- #
    def _needs_confirm(self, meta, old, new):
        rule = meta.get("confirm", "never")
        if rule == "never":
            return False
        if rule == "always":
            return True
        if rule == "aggressive":
            below = meta.get("confirm_below")
            if below is None:
                return False
            # “更激进/更侵入”= 数值变得更极端（对间隔/间隔类，调小更侵入）
            return new < below
        return False

    def _same(self, meta, old, new):
        if meta["type"] in ("int", "float"):
            try:
                return abs(float(old) - float(new)) < 1e-6
            except Exception:
                return False
        if meta["type"] == "bool":
            return bool(old) == bool(new)
        if meta["type"] == "list":
            return list(old or []) == list(new or [])
        return old == new

    def _ask_confirm(self, meta, old, new, reason, category):
        """弹窗请你确认。无 gui / 超时 → 安全拒绝（fail-safe）。"""
        if self.gui is None:
            return False
        title = f"{CONFIG.get('name', '小念')} 想自主调整设置"
        msg = (f"{reason}\n\n"
               f"【{meta['label']}】\n"
               f"当前：{old}\n想改成：{new}\n\n"
               f"（仅修改配置文件，不动系统、不删文件、不改代码）\n"
               f"允许小念这样调整吗？")
        try:
            return bool(self.gui.request_confirm(title, msg))
        except Exception:
            return False

    def _backup_overrides(self):
        try:
            if os.path.exists(self.overrides_path):
                import shutil
                shutil.copyfile(self.overrides_path, self.overrides_path + ".bak")
        except Exception:
            pass

    def _audit(self, key, old, new, reason, confirmed, applied, category, note=""):
        meta = AUTONOMY_WHITELIST.get(key, {})
        row = {
            "ts": time.time(), "category": category, "key": key,
            "label": meta.get("label", key), "old": old, "new": new,
            "reason": reason, "confirmed": confirmed, "applied": applied, "note": note,
        }
        try:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self._log(f"{'生效' if applied else '未生效'} {category} {meta.get('label', key)} "
                  f"{old}→{new} | {reason}")

    def _log(self, msg):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
