# -*- coding: utf-8 -*-
"""
Qwen2.5 分词桥：概念(字符串) -> qwen 词表 token id。

token 级介入的基础设施：意识层选出的概念要转成 token id
才能通过 logit_bias 直接拧解码时的 logits。
带缓存；tokenizer.json 加载一次全局复用。
"""
from __future__ import annotations
from typing import List, Dict
import os
import re

_TOKENIZER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qwen2.5_tokenizer.json")
_tok = None
_cache: Dict[str, List[int]] = {}

# 不值得 bias 的 token：纯标点/纯空白解码结果
_JUNK = re.compile(r"^[\s\W_]+$", re.UNICODE)


def _get():
    """惰性加载 qwen 分词器；tokenizers 库缺失时抛 ImportError（由 available() 兜住）。"""
    global _tok
    if _tok is None:
        from tokenizers import Tokenizer
        _tok = Tokenizer.from_file(_TOKENIZER_PATH)
    return _tok


def available() -> bool:
    return os.path.exists(_TOKENIZER_PATH)


def encode(concept: str) -> List[int]:
    """概念 -> token ids（无特殊符号）。带缓存。"""
    if concept in _cache:
        return _cache[concept]
    ids = _get().encode(concept, add_special_tokens=False).ids
    _cache[concept] = ids
    return ids


def concept_token_ids(concept: str, max_tokens_per_concept: int = 4) -> List[int]:
    """
    取概念的 token id，过滤垃圾 token(纯标点)，最多取前 N 个。
    首 token 最关键(决定模型会不会'起这个念头')，多 token 概念全部前几个都给 bias，
    这样解码到一半时后续 token 也被顺势抬高，概念更容易完整出现。
    """
    tok = _get()
    out: List[int] = []
    for tid in encode(concept)[:max_tokens_per_concept]:
        piece = tok.decode([tid])
        if piece and not _JUNK.match(piece):
            out.append(tid)
    return out
