# -*- coding: utf-8 -*-
"""
token 级介入核心：把意识层的概率分布编译成 logit_bias。

对应理论的落点从"提示词软引导"下沉到"解码硬介入"：
  - 意识层算出 概念->选取概率 P(c)。
  - bias(token) = BIAS_GAIN * P(c)^BIAS_GAMMA，再 clamp 到 [0, BIAS_MAX]。
    => 权重占比大的概念，其 token 在 qwen 每一步解码里 logits 被抬得越高，
       被'调用并组合'进语言输出的概率就越大；权重小的抬得少 —— 严格正相关。
  - 被伪随机组合选中的"这一念"概念额外乘 CHOSEN_BOOST（当下的念头最响亮）。
  - 条目数有限（工作记忆宽度），按 bias 从大到小截断。

这样 transformer 的 softmax(logits) 每一步都被意识层的统计+链式+概率结构直接调制。
"""
from __future__ import annotations
from typing import Dict

import cl_config as config
import qwen_tokenizer as qtok


def build_logit_bias(state) -> Dict[str, float]:
    """ConsciousState -> {token_id(str): bias(float)}，供 /v1/chat/completions 使用。"""
    if not config.TOKEN_BIAS_ENABLED or not qtok.available():
        return {}
    if not state.distribution:
        return {}

    chosen = {c for c, _ in state.chosen}
    seeds = set(state.seeds)
    bias_map: Dict[int, float] = {}

    # 主念概念：满额 boost；副念概念：按 SECONDARY_BIAS_FRACTION 折让（仍属"被唤醒的联想"）
    primary_set = chosen
    secondary_set = set()
    if state.thoughts:
        for th in state.thoughts:
            if th.is_primary:
                continue
            for c, _ in th.concepts:
                secondary_set.add(c)

    def _chosen_boost(concept: str) -> float:
        if concept in primary_set:
            return config.BIAS_CHOSEN_BOOST
        if concept in secondary_set:
            return 1.0 + (config.BIAS_CHOSEN_BOOST - 1.0) * config.SECONDARY_BIAS_FRACTION
        return 1.0

    for concept, p in state.distribution.items():
        # 种子概念(用户输入里已有的词)不偏置：模型本来就看得见它们。
        # 真正要拉进输出的是"链式解锁出的联想概念"——输入里没有、意识里被唤醒的。
        if concept in seeds:
            continue
        b = config.BIAS_GAIN * (p ** config.BIAS_GAMMA)
        cb = _chosen_boost(concept)
        if cb > 1.0:
            # 事件强度放大：这一念由越多越强(强度大)的词组成，其 token 偏置越响亮。
            # 用饱和因子 frac=E/(E+REF) 把事件强度压到 [0,1)，避免数值大把 bias 顶到上限。
            frac = state.event_strength / (state.event_strength + config.EVENT_STRENGTH_REF)
            b *= cb * (1.0 + config.EVENT_BIAS_GAIN * frac)
        else:
            b *= cb
        b = min(config.BIAS_MAX, b)
        if b < config.BIAS_MIN_EFFECTIVE:
            continue
        for tid in qtok.concept_token_ids(concept):
            # 同一 token 被多个概念命中时取最大（不叠加，防止爆炸）
            bias_map[tid] = max(bias_map.get(tid, 0.0), b)

    # 条目截断：保留 bias 最大的 N 个（工作记忆宽度）
    if len(bias_map) > config.BIAS_MAX_ENTRIES:
        top = sorted(bias_map.items(), key=lambda kv: kv[1], reverse=True)[: config.BIAS_MAX_ENTRIES]
        bias_map = dict(top)

    return {str(k): round(v, 2) for k, v in bias_map.items()}
