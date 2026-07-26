# -*- coding: utf-8 -*-
"""
离线中文分词（零依赖：只读取打包的 jieba 词表 dict.txt，不需要 jieba 包，不联网）。

为什么要有它：
  小念本地版装不了 jieba（无外网），此前感知端只能把中文切成「单字/2字 ngram」，
  导致关联图是一张几千常用字两两相连的毛线团，概念毫无意义、价值词（学习/熬夜）也命中不了。
  这里用一个 jieba 的完整词表，实现经典的「DAG + 最大概率路径(Viterbi)」切词，
  把文本切成真实词汇——节点从百万级 ngram 降到万级真词，图立刻变得稀疏、有意义。

算法：
  - 加载 dict.txt（格式：词 词频 词性）建 词->(词频,词性) 查表，并记录最长词长。
  - 对每个中文片段建 DAG：从位置 i 出发，所有落在词表里的结束位置 j 都是候选边。
  - Viterbi 沿 DAG 求「log(词频) + 词长微偏置」最大的切分路径，即最可能分词。
    （等价于 jieba 核心切词，不含 HMM 新词发现——对概念抽取足够。）
  - cut() 返回词序列；cut_with_pos() 额外返回词性（供原感知模块的 _pos_weight 复用）。

若词表缺失，available() 返回 False，调用方自动回退到旧 ngram 方案，绝不崩溃。
"""
from __future__ import annotations
import os
import re

_DICT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jieba_dict.txt")

_word_freq: dict = {}      # word -> freq
_word_pos: dict = {}       # word -> pos（词性标记，供显著度权重）
_loaded = False
_max_len = 0

_CJK = re.compile(r"[一-鿿]+")   # 中文片段（CJK 统一表意文字基本区）


def load() -> None:
    """惰性加载词表到内存（仅一次）。词表缺失则记为空，available()=False。"""
    global _loaded, _max_len
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(_DICT_PATH):
        return
    with open(_DICT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            w = parts[0]
            try:
                freq = int(parts[1])
            except ValueError:
                freq = 1
            pos = parts[2] if len(parts) >= 3 else "n"
            # 词表可能重复词条，取较大词频
            if freq > _word_freq.get(w, 0):
                _word_freq[w] = freq
                _word_pos[w] = pos
                if len(w) > _max_len:
                    _max_len = len(w)


def available() -> bool:
    load()
    return bool(_word_freq)


def _build_dag(s: str):
    """为片段 s 建 DAG：dag[i] = [j1, j2, ...]，s[i:j] 落在词表或单字兜底。"""
    n = len(s)
    dag = [[] for _ in range(n)]
    for i in range(n):
        # 单字兜底（即使不在词表也允许切出，词频默认为 1）
        dag[i].append(i + 1)
        # 词表里的多字词
        maxj = min(n, i + _max_len + 1)
        for k in range(i + 2, maxj + 1):
            if s[i:k] in _word_freq:
                dag[i].append(k)
    return dag


def _route(s: str, dag):
    """
    前向最大匹配：在每个位置贪心取「最长的词表词」切出，没有词表词才退化为单字。
    选择「最长词表词」而非 freq-Viterbi，是因为本词表单字频次偏高，
    freq-Viterbi 会把「游戏」拆成「游/戏」（单字累计分更高），而最大匹配能稳定产出真实词汇。
    对概念抽取，我们要的正是"尽量长的真词"，故最大匹配比纯概率更合适。
    dag[i] 为升序候选结束位（单字兜底 i+1 在最前，多字词在后）。
    """
    n = len(s)
    toks = []
    i = 0
    while i < n:
        ends = dag[i]  # 升序
        # 优先取「落在词表里」的最长候选（多字词排在单字兜底之后）
        dict_ends = [j for j in ends if s[i:j] in _word_freq]
        chosen = dict_ends[-1] if dict_ends else ends[-1]
        toks.append(s[i:chosen])
        i = chosen
    return toks


def cut(text: str):
    """离线切词：返回中文词序列（英文/标点被跳过，由调用方另行处理）。"""
    load()
    out = []
    for seg in _CJK.findall(text or ""):
        if not _word_freq:
            out.extend(list(seg))   # 无词表：退化为单字
            continue
        out.extend(_route(seg, _build_dag(seg)))
    return out


def cut_with_pos(text: str):
    """离线切词并附带词性：返回 [(word, pos), ...]，供感知模块按词性给显著度。"""
    load()
    out = []
    for seg in _CJK.findall(text or ""):
        if not _word_freq:
            out.extend((c, "x") for c in seg)
            continue
        for w in _route(seg, _build_dag(seg)):
            out.append((w, _word_pos.get(w, "n")))
    return out


if __name__ == "__main__":
    # 自测
    demo = "我今天想熬夜打游戏，但明天还要考试得好好学习"
    print("available:", available())
    print("cut:", cut(demo))
    print("cut_with_pos:", cut_with_pos(demo))
