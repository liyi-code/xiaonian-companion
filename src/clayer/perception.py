# -*- coding: utf-8 -*-
"""
感知模块：把一段文本切成"概念"(信息单元)，并给每个概念一个「显著度权重」。

意识层的输入端：外界文字 -> 带权重、带词序的概念序列。

粒度升级（三层）：
  1) 词性过滤：用 jieba.posseg 只保留"实词"(名词/动词/形容词/专名/时空词…)，
     用词性直接滤掉助词/代词/介词/连词/量词等虚词，比手写停用词名单干净得多。
  2) 显著度加权：概念权重 = 词性权重 × 词长加成 × 复现加成。
     专名/名词权重高、单字虚词权重低；越长、在句中出现越多的概念越"显眼"。
  3) 保留词序：extract_weighted 返回**有序**序列，供下游按"邻近"建立更强的关联边。

对外接口：
  extract(text)          -> List[str]              (兼容旧调用，只回概念名，去重保序)
  extract_weighted(text) -> List[Tuple[str,float]] (概念名 + 显著度权重，保留出现顺序)

分词优先级：jieba.posseg（词性+实词过滤）> 离线分词 seg（打包 jieba 词表的 DAG 最大概率切词，
              零依赖、无外网也能切出真实词汇并带词性）> jieba.cut > 纯 ngram 兜底。
离线分词让"无 jieba"环境也能产出稀疏、有意义的真词概念，避免关联图退化成字级毛线团。
"""
from __future__ import annotations
from typing import List, Tuple, Dict
import re

import cl_config as config

_HAS_POSSEG = False
_HAS_JIEBA = False
try:
    import jieba  # 可选
    _HAS_JIEBA = True
    try:
        import jieba.posseg as _pseg  # 词性标注
        _HAS_POSSEG = True
    except Exception:
        _HAS_POSSEG = False
except Exception:
    _HAS_JIEBA = False

# 离线分词（零依赖：打包的 jieba 词表 dict.txt + DAG 最大概率切词）。
# 优先级低于 jieba 但高于纯 ngram 兜底——没有 jieba 包/无外网时，照样切出真实词汇。
try:
    from seg import cut_with_pos as _seg_cut_with_pos, available as _seg_available
    _SEG_OK = _seg_available()
except Exception:
    _SEG_OK = False
    _seg_cut_with_pos = None

# 常见中英文停用词(精简版，作为词性过滤之外的兜底)
_STOP = set("""
的 了 和 是 在 我 你 他 她 它 我们 你们 他们 这 那 也 就 都 而 及 与 或 一个 一 不 没 有 吗 呢 吧 啊 哦 嗯
着 过 得 地 把 被 让 使 于 之 其 且 但 却 还 又 再 很 太 更 最 会 能 要 想 说 对 从 向 到 给 用 为
the a an of to in on and or is are was were be been being it this that these those i you he she we they
for with as at by an be do does did not no yes so if then than too very can will would should could
是不是 东西 正在 整个 这个 那个 什么 怎么 可以 一些 这样 那样 因为 所以 但是 如果 然后 这些 那些 一下 一点
""".split())

_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

# ---------- 词性 -> 基础显著度权重 ----------
# jieba 词性表：n名词 nr人名 ns地名 nt机构 nz其他专名 v动词 vn名动词 a形容词 an名形词
#              t时间 s处所 f方位 z状态词 b区别词 m数词 q量词 eng英文
#              r代词 d副词 p介词 c连词 u助词 e叹词 y语气 o拟声 w标点 x非语素
_POS_WEIGHT: Dict[str, float] = {
    # 专有名词 / 实体：信息量最高
    "nr": 1.6, "ns": 1.6, "nt": 1.6, "nz": 1.5, "nrt": 1.6, "nrfg": 1.6,
    # 名词
    "n": 1.3, "nl": 1.3, "ng": 1.15, "an": 1.15, "vn": 1.2,
    # 动词
    "v": 1.0, "vd": 0.9, "vg": 0.85, "vi": 0.95, "vq": 0.9,
    # 形容词
    "a": 1.0, "ad": 0.85, "ag": 0.85, "al": 1.0,
    # 时间 / 处所 / 状态 / 区别（方位词 f 归入虚词丢弃）
    "t": 0.85, "s": 0.85, "z": 0.9, "b": 0.8,
    # 数量（弱信息）
    "m": 0.55, "mq": 0.55, "q": 0.4,
    # 英文
    "eng": 1.2, "x": 0.6,
}
# 明确视为"虚词"直接丢弃的词性首字母
_DROP_POS_PREFIX = set("rdpcuyeowf")  # 代词/副词/介词/连词/助词/语气/叹词/拟声/标点/方位
# 特殊：这些高频虚动词(是/有)即使 v 开头也丢
_DROP_WORDS = {"是", "有", "在", "会", "能", "要", "想", "说", "让", "把", "被", "给", "对", "用", "为", "来", "去", "做"}


def _pos_weight(flag: str) -> float:
    """按词性标记返回基础权重；虚词返回 0(丢弃)。"""
    if not flag:
        return 1.0
    f = flag.lower()
    if f in _POS_WEIGHT:
        return _POS_WEIGHT[f]
    # 两字标记未命中，退到首字母判断
    head = f[0]
    if head in _DROP_POS_PREFIX:
        return 0.0
    # 名/动/形/时/处 等大类兜底
    fallback = {"n": 1.2, "v": 0.95, "a": 0.95, "t": 0.8, "s": 0.8}.get(head)
    return fallback if fallback is not None else 0.7


def _len_bonus(word: str) -> float:
    """词长加成：越长的词通常越具体、信息量越大；单字略降。"""
    if word.isascii():
        n = len(word)
    else:
        n = len(word)  # 中文按字数
    if n <= 1:
        return 0.8
    if n == 2:
        return 1.0
    if n == 3:
        return 1.15
    return 1.25  # >=4


def _cjk_ngrams(seg: str) -> List[Tuple[str, float]]:
    """无 jieba 时：单字(弱) + 相邻二元组(强)，兼顾召回与一点点结构。"""
    out: List[Tuple[str, float]] = []
    chars = list(seg)
    for ch in chars:
        out.append((ch, 0.6))
    for i in range(len(chars) - 1):
        out.append((chars[i] + chars[i + 1], 1.0))
    return out


def extract_weighted(text: str, max_concepts: int = 40) -> List[Tuple[str, float]]:
    """
    从文本抽取「概念 + 显著度权重」，保留出现顺序，累加复现权重。
    权重 = 词性权重 × 词长加成（同词复现则权重累加，越常提及越显眼）。
    """
    if not text:
        return []
    text = text.strip()

    order: List[str] = []          # 首次出现顺序
    weight: Dict[str, float] = {}  # 概念 -> 累计权重

    def _add(word: str, w: float):
        word = word.strip()
        if not word or word in _STOP:
            return
        if w <= 0:
            return
        if word not in weight:
            weight[word] = 0.0
            order.append(word)
        weight[word] += w

    if _HAS_POSSEG and config.PERCEPTION_USE_POS:
        for tok in _pseg.cut(text):
            w, flag = tok.word, tok.flag
            w = w.strip()
            if not w:
                continue
            if w in _DROP_WORDS:
                continue
            base = _pos_weight(flag)
            if base <= 0:
                continue
            if _CJK.search(w) or _EN_WORD.fullmatch(w):
                key = w.lower() if w.isascii() else w
                _add(key, base * _len_bonus(key))
    elif _SEG_OK:
        # 离线分词（打包 jieba 词表，无 jieba 包也能切出真实词汇，并带词性）
        for w, flag in _seg_cut_with_pos(text):
            w = w.strip()
            if not w:
                continue
            if w in _DROP_WORDS:
                continue
            base = _pos_weight(flag)
            if base <= 0:
                continue
            if _CJK.search(w):
                _add(w, base * _len_bonus(w))
    elif _HAS_JIEBA:
        for w in jieba.cut(text):
            w = w.strip()
            if not w or w in _STOP or w in _DROP_WORDS:
                continue
            if _CJK.search(w) or _EN_WORD.fullmatch(w):
                key = w.lower() if w.isascii() else w
                _add(key, _len_bonus(key))
    else:
        # 英文词
        for m in _EN_WORD.findall(text):
            _add(m.lower(), 1.1 * _len_bonus(m))
        # 中文片段 -> ngram（最后兜底，仅在没有 jieba 也没有离线词表时）
        for seg in _CJK.findall(text):
            for g, gw in _cjk_ngrams(seg):
                _add(g, gw)

    # 过最小权重阈值 + 按"权重降序"截断（保留最显著的），但输出仍按出现顺序
    kept = {c for c in order if weight[c] >= config.PERCEPTION_MIN_WEIGHT}
    if len(kept) > max_concepts:
        top = sorted(kept, key=lambda c: weight[c], reverse=True)[:max_concepts]
        kept = set(top)
    return [(c, round(weight[c], 4)) for c in order if c in kept]


def extract(text: str, max_concepts: int = 40) -> List[str]:
    """兼容旧接口：只回概念名(去重、保序、过滤停用词/虚词)。"""
    return [c for c, _ in extract_weighted(text, max_concepts=max_concepts)]
