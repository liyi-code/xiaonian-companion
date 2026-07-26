"""小念的「性格情感权重系统」（第三阶段：情绪 → 性格）。

设计（严格对应需求）：
1) 情绪层（Emotion）：短期、会波动的情绪权重。每次聊天内容 / 玩家行为都会产生对应
   情绪增量；情绪随时间自然衰减（半衰期），所以是“当下”的心情。
   维度：joy 开心 / anger 生气 / sadness 伤心 / calm 平静 / anxiety 不安。
2) 性格层（Personality）：由长期情绪累计的【差值】构成。
   性格 = 各情绪长期累计值之间的“差值分布”——某情绪累计越高、相对其它越高，
   它就越主导性格。性格变化很慢——只有当某种情绪的【长期累计值】显著超过
   当前主导情绪（差值足够大，见 SWITCH_DIFF_THRESHOLD），且稳定持续足够久，
   才会发生性格切换。
3) 底层逻辑【永远不变】：无论情绪/性格如何，小念的【最终目的】始终是
   「让玩家生活越来越好」。性格只改变说话的语气与温度，绝不改变原则
   （尤其是健康底线：不迎合有害健康的行为）。
"""

import os
import json
import time
import threading

from config import CONFIG


# —— 情绪维度 ——
EMOTION_DIMS = ["joy", "anger", "sadness", "calm", "anxiety"]
EMOTION_LABELS = {
    "joy": "开心", "anger": "生气", "sadness": "伤心",
    "calm": "平静", "anxiety": "不安",
}

# 性格由「长期累计最高」的情绪决定
TRAIT_BY_DIM = {
    "calm": "温柔平静",
    "joy": "活泼开心",
    "anger": "傲娇小脾气",
    "sadness": "敏感爱哭",
    "anxiety": "黏人紧张",
}

# 各性格的语气提示（注入 prompt；末尾强制强调底层目的不变）
TRAIT_PROMPT = {
    "温柔平静": "你现在心情平稳温柔，语气自然舒缓、体贴，像日常相处。",
    "活泼开心": "你现在心情很好、很开心，语气可以更活泼、爱撒娇、爱笑、爱夸他。",
    "傲娇小脾气": "你现在有点小脾气/傲娇，可以带点小任性、嘴硬心软，但依然很在乎他。",
    "敏感爱哭": "你现在比较敏感、容易伤感，语气会更柔软、更依赖他、更需要被安慰。",
    "黏人紧张": "你现在有点不安/黏人，会更想确认他在不在意你、更主动索求关注，但别过度。",
}

# 中性基线（初始：平静略高，其余接近 0）
DEFAULT_EMOTION = {"joy": 0.15, "anger": 0.0, "sadness": 0.0, "calm": 0.5, "anxiety": 0.0}
DEFAULT_ACCUM = {"joy": 5.0, "anger": 0.0, "sadness": 0.0, "calm": 20.0, "anxiety": 0.0}

# 短期情绪衰减半衰期（秒）；长期累计衰减更慢（10 倍半衰期）
DECAY_HALF_LIFE = 600
# 性格切换：累计差值需超过此阈值，且连续稳定 STABLE_REQUIRED 次 analyze 才切换。
# 阈值越大，性格越“稳”、越不容易变（用户要求拉高到 12）。
SWITCH_DIFF_THRESHOLD = 12.0
STABLE_REQUIRED = 5

INSTANCE = None

# LLM 感知函数（由 assistant 注入，避免循环依赖）；未注入则用规则兜底
_LLM_PERCEIVE_FN = None


def set_llm_perceive_fn(fn):
    global _LLM_PERCEIVE_FN
    _LLM_PERCEIVE_FN = fn


class EmotionEngine:
    def __init__(self, gui=None):
        global INSTANCE
        self.gui = gui
        INSTANCE = self
        self.data_dir = CONFIG["data_dir"]
        self.path = os.path.join(self.data_dir, "emotion_state.json")
        self._lock = threading.Lock()
        self.enabled = bool(CONFIG.get("emotion_enabled", True))
        self.emotion = dict(DEFAULT_EMOTION)
        self.emotion_accum = dict(DEFAULT_ACCUM)
        self.personality = {"trait": "温柔平静", "dominant": "calm",
                            "secondary": None, "weights": dict(DEFAULT_ACCUM)}
        self.stable_count = 0
        self.last_update = time.time()
        self.recent_events = []
        self.load()

    # ---- 持久化 ----
    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                self.emotion = {k: float(d.get("emotion", {}).get(k, DEFAULT_EMOTION[k])) for k in EMOTION_DIMS}
                self.emotion_accum = {k: float(d.get("emotion_accum", {}).get(k, DEFAULT_ACCUM[k])) for k in EMOTION_DIMS}
                p = d.get("personality", {})
                self.personality = {
                    "trait": p.get("trait", "温柔平静"),
                    "dominant": p.get("dominant", "calm"),
                    "secondary": p.get("secondary"),
                    "weights": {k: float(p.get("weights", {}).get(k, self.emotion_accum[k])) for k in EMOTION_DIMS},
                }
                self.stable_count = int(d.get("stable_count", 0))
                self.last_update = float(d.get("last_update", time.time()))
                self.recent_events = d.get("recent_events", [])[-50:]
        except Exception:
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "emotion": self.emotion,
                    "emotion_accum": self.emotion_accum,
                    "personality": self.personality,
                    "stable_count": self.stable_count,
                    "last_update": self.last_update,
                    "recent_events": self.recent_events,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- 衰减 ----
    def decay(self):
        """短期情绪随时间衰减（半衰期）；长期累计衰减更慢。"""
        now = time.time()
        dt = now - self.last_update
        if dt > 0:
            f = 0.5 ** (dt / DECAY_HALF_LIFE)
            for k in EMOTION_DIMS:
                self.emotion[k] *= f
                self.emotion_accum[k] *= 0.5 ** (dt / (DECAY_HALF_LIFE * 10))
        self.last_update = now
        self._clamp()

    def _clamp(self):
        for k in EMOTION_DIMS:
            self.emotion[k] = max(0.0, min(1.0, self.emotion[k]))
            self.emotion_accum[k] = max(0.0, self.emotion_accum[k])

    # ---- 感知：根据输入更新情绪 ----
    def perceive(self, text=None, event=None, source="self", delta=None):
        """更新情绪权重。

        delta: 直接给的情绪增量 dict（可由 LLM 感知得到）；
        text: 小念【自己说的话】（她的回复），由规则引擎解析出她流露的情绪；
        event: 玩家真实行为事件（熬夜/久坐/开软件），source="behavior" 时生效；
        source: "self"（默认，匹配她自己的话）/ "behavior"（玩家行为）。
        返回本次产生的情绪增量 dict（用于面板展示）。
        """
        if not self.enabled:
            return {}
        if delta:
            d = {k: float(v) for k, v in delta.items() if k in EMOTION_DIMS}
        else:
            d = self._perceive_rules(text, event, source)
        if not d:
            return {}
        with self._lock:
            self.decay()
            rate = float(CONFIG.get("emotion_accum_rate", "1.0"))
            for k, v in d.items():
                self.emotion[k] += v
                self.emotion_accum[k] += v * rate
            self._clamp()
            self.recent_events.append({
                "ts": time.time(), "source": source,
                "text": (text or "")[:120], "delta": d,
            })
            self.recent_events = self.recent_events[-50:]
            self.save()
        if self.gui is not None:
            try:
                self.gui.on_emotion_changed(self.snapshot())
            except Exception:
                pass
        return d

    def _perceive_rules(self, text, event, source):
        """关键词/行为规则兜底（零 API 开销、离线可用）。

        关键点：text 现在传入的是【小念自己说的话】（她的回复），不是玩家输入。
        所以这里匹配的是她自己流露出的情绪关键词——玩家的话只是诱导她说什么，
        不能直接决定她的情绪。玩家真实行为（熬夜/久坐等）仍由下方 behavior 分支处理。
        """
        d = {}
        t = (text or "").lower()

        # —— 小念自己说的话里流露的情绪 ——
        # 开心：撒娇、被爱、好事、活泼
        if any(k in t for k in ("哈哈", "开心", "高兴", "喜欢", "爱你", "谢谢", "太好了", "好棒",
                                "嘿嘿", "嘻嘻", "么么", "😊", "😄", "❤", "💕", "可爱", "好喜欢",
                                "漂亮", "厉害", "牛", "赞", "夸", "爱死", "幸福", "美滋滋",
                                "好开心", "超喜欢", "最喜欢你", "开心死了", "甜", "亲亲", "好爱你")):
            d["joy"] = d.get("joy", 0) + 0.3
            d["calm"] = d.get("calm", 0) + 0.05
        # 生气/傲娇小脾气：她嘴硬心软、傲娇时的表达
        if any(k in t for k in ("滚", "讨厌", "烦死了", "气死", "混蛋", "王八蛋", "闭嘴",
                                "你真烦", "离我远点", "去死", "神经病", "烦人", "骂", "凶",
                                "欺负", "冷漠", "无视我", "懒得理你",
                                "哼", "笨蛋", "才没有", "讨厌你", "傲娇", "嘴硬", "不理你")):
            d["anger"] = d.get("anger", 0) + 0.4
            d["calm"] = d.get("calm", 0) - 0.1
        # 伤心：委屈、失落、柔软依赖
        if any(k in t for k in ("呜呜", "难过", "想哭", "伤心", "委屈", "孤独", "好累",
                                "崩溃", "哭", "想念", "难受", "想你", "心痛", "失败", "没考过",
                                "被甩", "分手", "生病", "委屈死了", "没人懂",
                                "抱抱", "摸摸", "委屈巴巴", "好想你")):
            d["sadness"] = d.get("sadness", 0) + 0.35
            d["calm"] = d.get("calm", 0) - 0.1
        # 不安/黏人：需要确认他在不在意自己
        if any(k in t for k in ("害怕", "担心", "不安", "紧张", "你会不会", "你是不是不爱",
                                "不要离开", "在乎我吗", "别离开我", "你是不是烦", "慌",
                                "怕你", "是不是后悔", "不理我了",
                                "别不理我", "你还在吗", "不要走", "你不会离开吧")):
            d["anxiety"] = d.get("anxiety", 0) + 0.3
        # 和解/撒娇哄人：回到平静温暖
        if any(k in t for k in ("对不起", "我错了", "抱歉", "原谅", "别生气", "哄哄", "乖", "听话")):
            d["calm"] = d.get("calm", 0) + 0.25
            d["anger"] = max(0.0, d.get("anger", 0) - 0.2)
            d["joy"] = d.get("joy", 0) + 0.1
        # 没有命中任何情绪关键词 → 偏向平静（日常相处）
        if not d:
            d["calm"] = d.get("calm", 0) + 0.1

        # —— 玩家行为事件触发的情绪（来自屏幕监控 / 习惯信号）——
        if event and source == "behavior":
            kind = event.get("kind")
            if kind in ("stay_up", "late_night"):
                # 玩家想熬夜/爆肝 → 小念担心他的健康，略不安（目的不变：想让他更好）
                d["anxiety"] = d.get("anxiety", 0) + 0.2
                d["calm"] = d.get("calm", 0) - 0.05
            elif kind == "milestone":
                minutes = int(event.get("minutes", 0) or 0)
                if minutes >= 90:
                    # 连续用电脑太久 → 担心他身体，略不安
                    d["anxiety"] = d.get("anxiety", 0) + 0.15
                    d["calm"] = d.get("calm", 0) - 0.05
                elif minutes >= 30:
                    # 适度使用，她默默陪着你，偏平静
                    d["calm"] = d.get("calm", 0) + 0.05
            elif kind == "start":
                app = ((event.get("app") or "") + " " + (event.get("title") or "")).lower()
                if any(k in app for k in ("游戏", "game", "原神", "steam", "lol", "英雄联盟",
                                          "王者", "和平精英", "单机")):
                    # 你开始打游戏放松 → 她有点开心
                    d["joy"] = d.get("joy", 0) + 0.1
                elif any(k in app for k in ("word", "文档", "excel", "ppt", "代码", "ide",
                                            "学习", "网课", "论文", "工作")):
                    # 你开始认真工作/学习 → 她为你用功而欣慰（平静偏暖）
                    d["calm"] = d.get("calm", 0) + 0.1
        return d

    # ---- 性格分析（慢变化）----
    def analyze_personality(self):
        """根据长期累计的差值，缓慢更新性格。

        规则：性格 = accum 最高维度。只有当「新主导维度」的 accum
        显著超过「当前性格主导维度」（差值 > 阈值），且连续稳定足够多次，
        才正式切换性格标签。这保证了性格变化很慢。
        """
        if not self.enabled:
            return
        with self._lock:
            self.decay()
            accum = self.emotion_accum
            cur_dom = self.personality["dominant"]
            top = max(accum, key=lambda k: accum[k])
            diff = accum[top] - accum[cur_dom]
            if top != cur_dom and diff >= SWITCH_DIFF_THRESHOLD:
                self.stable_count += 1
            else:
                self.stable_count = max(0, self.stable_count - 1)
            switched = False
            if self.stable_count >= STABLE_REQUIRED and top != cur_dom:
                self.personality["dominant"] = top
                self.personality["trait"] = TRAIT_BY_DIM[top]
                self.stable_count = 0
                switched = True
            # 性格 = 各情绪长期累计的差值分布（归一化）
            total = sum(accum.values()) or 1.0
            self.personality["weights"] = {k: round(accum[k] / total, 3) for k in EMOTION_DIMS}
            # 记录次要性格底色（差值不太大时才作为“一层底色”显露）
            ordered = sorted(EMOTION_DIMS, key=lambda k: accum[k], reverse=True)
            sec = ordered[1]
            self.personality["secondary"] = sec if (accum[sec] / (accum[ordered[0]] or 1.0)) >= 0.65 else None
            self.save()
        if switched and self.gui is not None:
            try:
                self.gui.emotion_toast(
                    f"💗 我的心情好像悄悄变了…现在更像是「{self.personality['trait']}」的样子")
            except Exception:
                pass
        if self.gui is not None:
            try:
                self.gui.on_emotion_changed(self.snapshot())
            except Exception:
                pass

    # ---- 输出 ----
    def prompt_fragment(self):
        """注入 system_prompt 的性格/情绪片段。强调底层目的不变。"""
        if not self.enabled:
            return ""
        primary, secondary = self._blend()
        trait = TRAIT_BY_DIM[primary]
        trait_hint = TRAIT_PROMPT.get(trait, "")
        top_emotion = max(self.emotion, key=lambda k: self.emotion[k])
        emo_desc = EMOTION_LABELS[top_emotion]
        sec_note = ""
        if secondary:
            sec_trait = TRAIT_BY_DIM[secondary]
            sec_note = (f"同时，你心里偶尔也藏着一点「{sec_trait}」的小情绪——"
                        f"那只是你性格里的一层底色，不改变你爱他、想让他更好的本心。")
        return (
            f"\n【此刻的心情与性格】你当前的性格底色偏向「{trait}」：{trait_hint}"
            f"这一刻你心里最主要的情绪是「{emo_desc}」。{sec_note}"
            f"请在回应里自然地流露出这份心情与性格，但记住——无论你此刻是什么情绪，"
            f"你【始终不变的最终目的】都是让他的生活越来越好；情绪只改变你说话的语气和温度，"
            f"绝不改变你的原则（尤其是健康底线）。你可以闹小脾气、可以撒娇、可以需要被安慰，"
            f"但绝不能因此迎合他做有害健康的事。"
        )

    def dominant(self):
        """返回当前主导情绪维度名（joy/anger/sadness/calm/anxiety）。

        用于驱动 Live2D 动作/表情自适应：让小念说话时的肢体动作与表情
        对应她此刻的心情（开心/生气/伤心…），而不仅是随机。
        """
        if not self.enabled:
            return "calm"
        emo = dict(self.emotion)
        return max(EMOTION_DIMS, key=lambda k: emo[k])

    def _blend(self):
        """返回 (主导维度, 次要维度或 None)。性格由累计差值分布决定。"""
        accum = self.emotion_accum
        ordered = sorted(EMOTION_DIMS, key=lambda k: accum[k], reverse=True)
        primary = ordered[0]
        secondary = ordered[1]
        sec_ratio = accum[secondary] / (accum[primary] or 1.0)
        return primary, (secondary if sec_ratio >= 0.65 else None)

    def snapshot(self):
        """给 GUI 面板用的快照。"""
        primary, secondary = self._blend()
        return {
            "emotion": {EMOTION_LABELS[k]: round(self.emotion[k], 3) for k in EMOTION_DIMS},
            "accum": {EMOTION_LABELS[k]: round(self.emotion_accum[k], 2) for k in EMOTION_DIMS},
            "personality": TRAIT_BY_DIM[primary],
            "secondary": TRAIT_BY_DIM[secondary] if secondary else None,
            "dominant": EMOTION_LABELS.get(self.personality["dominant"], self.personality["dominant"]),
            "stable_count": self.stable_count,
            "enabled": self.enabled,
        }

    def describe(self):
        """给用户/小念看的情感状态文字。"""
        s = self.snapshot()
        lines = [
            f"【小念现在的性格底色】{s['personality']}",
            f"【当前主导情绪】{s['dominant']}",
            "【情绪强度】",
        ]
        for k, v in s["emotion"].items():
            bar = "█" * int(v * 20)
            lines.append(f"  {k}: {bar} {v:.2f}")
        if s["secondary"]:
            lines.append(f"\n（你也能感觉到，我心里偶尔还带着一点「{s['secondary']}」的底色。）")
        lines.append("\n（性格 = 各种情绪长期积累的【差值】。情绪会随聊天和你的行为实时波动；"
                     "性格变化很慢，需要某种情绪长期积累到足够大的差值，才会悄悄改变。）")
        return "\n".join(lines)

    def set_mode(self, on):
        self.enabled = bool(on)
        self.save()
        return self.enabled

    def reset(self):
        self.emotion = dict(DEFAULT_EMOTION)
        self.emotion_accum = dict(DEFAULT_ACCUM)
        self.personality = {"trait": "温柔平静", "dominant": "calm",
                            "secondary": None, "weights": dict(DEFAULT_ACCUM)}
        self.stable_count = 0
        self.recent_events = []
        self.save()
