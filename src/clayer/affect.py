# -*- coding: utf-8 -*-
"""
情绪 / 注意力 维度（多念竞争 / 意识流的"情感基调"与"注意资源分配"）。

对应理论：
  - 多念竞争：一轮意识里并行浮现至多 MAX_THOUGHTS 道"意念/意向"，它们并非平等——
    竞争力(vigor) 高的念赢得更多注意力资源，最终主念(注意力最高)主导语言输出，
    其余为"潜意识底色"。
  - 情绪维度（效价-唤醒 valence-arousal 二维模型）：
      效价 valence ∈ [-1,1]：消极 <-> 积极
      唤醒 arousal   ∈ [0,1] ：平静 <-> 激动
    每道念的情绪由其概念(情绪词库) + 全局情绪基调 + 该念的事件强度/熵共同推导。
    全局情绪有"惯性"，每轮只向主念情绪缓慢漂移 -> 形成连续的情绪流。
  - 注意力：把各念的竞争力做 softmax，分配注意力份额(和为1)。注意力越集中，
    主念越响亮、副念越潜；注意力越分散，多念越"平等地"共存于意识流。

该模块不依赖 LLM，纯数学；可独立单测。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple
import math

import cl_config as config


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


def _clamp01(x: float) -> float:
    return _clamp(x, 0.0, 1.0)


@dataclass
class Emotion:
    """效价-唤醒二维情绪。"""
    valence: float = 0.0    # [-1,1]
    arousal: float = 0.0    # [0,1]

    def label(self) -> str:
        if self.arousal < 0.33:
            base = "平静"
        elif self.arousal < 0.66:
            base = "温和"
        else:
            base = "强烈"
        if self.valence > 0.2:
            tone = "愉悦"
        elif self.valence < -0.2:
            tone = "低落"
        else:
            tone = "中性"
        return f"{base}{tone}"

    def to_dict(self) -> dict:
        return {"valence": self.valence, "arousal": self.arousal}

    @classmethod
    def from_dict(cls, d: dict) -> "Emotion":
        return cls(valence=float(d.get("valence", 0.0)), arousal=float(d.get("arousal", 0.0)))


# 内置核心情绪词库（极简，运行时可扩展）：概念命中的情绪偏置
_POS = {
    "喜欢", "爱", "开心", "快乐", "笑", "美好", "幸福", "温暖", "温柔", "舒服",
    "棒", "好", "美", "可爱", "惊喜", "期待", "满足", "安心", "感动", "甜",
    "治愈", "放松", "希望", "浪漫",
}
_NEG = {
    "怕", "恐惧", "痛", "哭", "难过", "悲伤", "失败", "讨厌", "烦", "愤怒",
    "焦虑", "孤独", "无聊", "累", "苦", "病", "死", "危险", "慌", "绝望",
    "寂寞", "委屈", "生气", "恨",
}
_HIGH_AROUSAL = {
    "惊", "突然", "急", "快", "炸", "激动", "兴奋", "慌", "愤怒", "怕",
    "冲", "尖叫", "震惊", "紧张", "热烈",
}


class AffectState:
    """全局情绪基调 + 概念情绪关联（轻量）。"""

    def __init__(self):
        self.valence = config.MOOD_VALENCE_INIT
        self.arousal = config.MOOD_AROUSAL_INIT

    # ---------- 概念级情绪 ----------
    def concept_emotion(self, c: str) -> Emotion:
        """单个概念的情绪：命中情绪词库则取词库基调，否则回退全局情绪。"""
        pos = c in _POS
        neg = c in _NEG
        if not pos and not neg:
            return Emotion(valence=self.valence, arousal=self.arousal)
        v = (1.0 if pos else 0.0) - (1.0 if neg else 0.0)
        ar = 1.0 if c in _HIGH_AROUSAL else 0.35
        return Emotion(valence=_clamp(v, -1.0, 1.0), arousal=_clamp01(ar))

    # ---------- 念级情绪（一道意念/意向） ----------
    def thought_emotion(self, concepts: List[Tuple[str, float]],
                        entropy: float, event_strength: float) -> Emotion:
        """
        一道念的情绪 = 概念情绪均值(效价) 与 唤醒(全局+词库+熵+事件强度 融合)。
        事件越强/意识越发散 -> 越"激动"；命中积极/消极词 -> 效价偏移。
        """
        hits = [self.concept_emotion(c) for c, _ in concepts]
        if hits:
            v = sum(h.valence for h in hits) / len(hits)
            ar_lex = sum(h.arousal for h in hits) / len(hits)
        else:
            v = self.valence
            ar_lex = self.arousal

        frac = event_strength / (event_strength + config.EVENT_STRENGTH_REF)
        arousal = (
            self.arousal * (1.0 - config.EMOTION_LEXICON_AROUSAL_W)
            + ar_lex * config.EMOTION_LEXICON_AROUSAL_W
            + entropy * config.EMOTION_ENTROPY_AROUSAL_W
            + frac * config.EMOTION_STRENGTH_AROUSAL_W
        )
        return Emotion(valence=_clamp(v, -1.0, 1.0), arousal=_clamp01(arousal))

    # ---------- 全局情绪漂移 ----------
    def update(self, seed_concepts: List[str], primary_emotion: Emotion) -> None:
        """全局情绪向主念情绪缓慢漂移（受惯性约束，形成连续情绪流）。"""
        sv = 0.0
        sn = 0
        for c in seed_concepts:
            if c in _POS or c in _NEG:
                sv += self.concept_emotion(c).valence
                sn += 1
        seed_v = sv / sn if sn > 0 else self.valence
        target_v = 0.5 * seed_v + 0.5 * primary_emotion.valence
        target_a = primary_emotion.arousal

        self.valence = _clamp(
            self.valence * config.MOOD_INERTIA + target_v * (1.0 - config.MOOD_INERTIA),
            -1.0, 1.0)
        self.arousal = _clamp01(
            self.arousal * config.MOOD_INERTIA + target_a * (1.0 - config.MOOD_INERTIA))

    # ---------- 注意力分配（竞争） ----------
    def allocate_attention(self, vigors: List[float], temp: float = config.ATTENTION_TEMP) -> List[float]:
        """
        把各念的竞争力 vigor 经 softmax 分配成注意力份额(和为1)。
        温度低 -> 注意力集中到最强念；温度高 -> 多念平分。
        """
        if not vigors:
            return []
        t = max(1e-6, temp)
        mx = max(vigors)
        ex = [math.exp((v - mx) / t) for v in vigors]
        z = sum(ex) or 1.0
        return [e / z for e in ex]

    # ---------- 持久化 ----------
    def to_dict(self) -> dict:
        return {"valence": self.valence, "arousal": self.arousal}

    @classmethod
    def from_dict(cls, d: dict) -> "AffectState":
        obj = cls()
        obj.valence = _clamp(float(d.get("valence", config.MOOD_VALENCE_INIT)), -1.0, 1.0)
        obj.arousal = _clamp01(float(d.get("arousal", config.MOOD_AROUSAL_INIT)))
        return obj
