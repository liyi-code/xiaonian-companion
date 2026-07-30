# -*- coding: utf-8 -*-
"""
logit_bias 量化验证（token 级介入的硬校验）。

为什么要有这个文件：
  token_bias.build_logit_bias 把意识层概率分布编译成 {token_id: bias}，
  直接喂给 LLM 的解码 logits。它目前是纯 Python，且没有任何测试——
  既没验证公式算得对，也没证明「喂给模型后解码真的被改变了」。
  意识层其它热路径(AssocGraph/MemoryStore)有 C++ parity 自检，
  但 logit_bias 这条「最后一公里」是盲区。本文件补上量化验证。

验证分两层：
  [A] 离线数值/性质检验（不依赖模型，秒级，确定性）：
      1) 公式正确性   bias = GAIN * p^GAMMA，clamp 到 [0, BIAS_MAX]，低于 MIN 丢弃
      2) 单调性       概率越高 -> bias 越高（未饱和区严格递增）
      3) 上限钳制     单 token bias 永不超过 BIAS_MAX
      4) 有效地板     低于 BIAS_MIN_EFFECTIVE 的概念不发送
      5) 种子排除     用户输入里已有的种子概念不偏置
      6) 选中增益     主念概念额外 ×CHOSEN_BOOST，且随事件强度饱和放大
      7) 次级增益     副念概念按 SECONDARY_BIAS_FRACTION 折让
      8) 条目截断     超过 BIAS_MAX_ENTRIES 只保留 bias 最大的 N 条
      9) 确定性       相同输入 -> 完全相同输出（含 2 位小数）
     10) 开关/空输入  TOKEN_BIAS_ENABLED=False 或空分布 -> {}
  [B] 在线模型效应测量（可选 --live，需要 llama-server / Ollama 在跑）：
      直接对模型发两次请求（bias=0 vs bias=BIAS_MAX），量化目标 token 的
      解码概率被抬高了多少倍（p_biased / p_base）。这才是「logit_bias 真的生效」
      的端到端证据；同时能复现「Ollama 的 logit_bias 形同虚设」这一已知结论。

运行：
  venv/Scripts/python.exe test_logit_bias.py            # 仅离线检验
  venv/Scripts/python.exe test_logit_bias.py --live     # 离线 + 在线
  venv/Scripts/python.exe test_logit_bias.py --target 音乐 --bias 8.0
"""
from __future__ import annotations
import math
import os
import sys

# 让 clayer 内部 import（cl_config / qwen_tokenizer / token_bias）可直接解析
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cl_config as config
import qwen_tokenizer as qtok
from token_bias import build_logit_bias


# ---------- 轻量状态替身（只暴露 build_logit_bias 实际读取的字段，避免拉起整层）----------
class _Thought:
    def __init__(self, concepts=None, is_primary=False):
        self.concepts = concepts or []   # [(concept, prob), ...]
        self.is_primary = is_primary


class _State:
    def __init__(self, distribution=None, chosen=None, seeds=None,
                 thoughts=None, event_strength=0.0):
        self.distribution = distribution or {}   # {concept: p}
        self.chosen = chosen or []               # [(concept, p)] 主念概念
        self.seeds = seeds or []                 # [concept] 用户输入种子
        self.thoughts = thoughts or []           # [_Thought]
        self.event_strength = event_strength


# ---------- 期望公式（镜像 token_bias.py 的实现，用于断言一致性）----------
def _expected_bias(p: float, boost_factor: float = 1.0, event_strength: float = 0.0):
    """复算单个概念应有的 bias（与 build_logit_bias 同公式）。返回 None 表示被丢弃。"""
    if p <= 0.0:
        return None
    b = config.BIAS_GAIN * (p ** config.BIAS_GAMMA)
    if boost_factor > 1.0:
        frac = event_strength / (event_strength + config.EVENT_STRENGTH_REF)
        b *= boost_factor * (1.0 + config.EVENT_BIAS_GAIN * frac)
    else:
        b *= boost_factor
    b = min(config.BIAS_MAX, b)
    if b < config.BIAS_MIN_EFFECTIVE:
        return None
    return round(b, 2)


def _first_tid(concept: str):
    """取概念首个有效 token id（用于从输出里找回该概念的 bias）。"""
    ids = qtok.concept_token_ids(concept)
    return ids[0] if ids else None


# ---------- 测试框架 ----------
_PASSED, _FAILED = [], []

def _check(name: str, cond: bool, detail: str = ""):
    if cond:
        _PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  — {detail}" if detail else ""))


# ============================================================
# [A] 离线数值 / 性质检验
# ============================================================
def test_formula_basic():
    print("\n[A1] 公式正确性  bias = GAIN * p^GAMMA (clamp + 地板)")
    # 选用未饱和区的概率，便于和期望精确比对
    cases = {
        "一": 0.04,    # 26*0.2 = 5.2
        "二": 0.10,    # 26*sqrt(0.1)=26*0.316=8.22 -> clamp 8.0
        "三": 0.01,    # 26*0.1 = 2.6
        "四": 0.001,   # 26*0.0316 = 0.82
        "五": 1e-4,    # 26*0.01 = 0.26 < 0.3 -> 丢弃
    }
    out = build_logit_bias(_State(distribution=cases, event_strength=0.0))
    for c, p in cases.items():
        exp = _expected_bias(p)
        tid = _first_tid(c)
        if exp is None:
            _check(f"公式/{c} 应被丢弃", tid is None or str(tid) not in out,
                   f"p={p} 期望丢弃")
        else:
            key = str(tid)
            got = out.get(key)
            _check(f"公式/{c}", got is not None and abs(got - exp) < 1e-9,
                   f"p={p} 期望={exp} 实际={got}")


def test_clamp_max():
    print("\n[A2] 上限钳制  bias <= BIAS_MAX")
    # 所有概念概率=1.0 都会被钳到 BIAS_MAX
    dist = {chr(0x4E00 + i): 1.0 for i in range(8)}
    out = build_logit_bias(_State(distribution=dist, event_strength=0.0))
    over = [v for v in out.values() if v > config.BIAS_MAX + 1e-9]
    any_hit = any(str(_first_tid(c)) in out for c in dist)
    _check("无条目超过 BIAS_MAX", not over, f"超过上限的={over}")
    _check("满概率概念被钳到 BIAS_MAX", any_hit and
           all(out[str(_first_tid(c))] == config.BIAS_MAX for c in dist
               if str(_first_tid(c)) in out),
           f"BIAS_MAX={config.BIAS_MAX}")


def test_floor():
    print("\n[A3] 有效地板  < BIAS_MIN_EFFECTIVE 的概念不发送")
    # 极小概率 -> bias 落到地板以下 -> 输出不应含其 token
    tiny = {chr(0x4E40 + i): 1e-7 for i in range(5)}
    out = build_logit_bias(_State(distribution=tiny, event_strength=0.0))
    present = [c for c in tiny if str(_first_tid(c)) in out]
    _check("地板以下概念全部丢弃", not present, f"误发的={present}")


def test_monotonic():
    print("\n[A4] 单调性  未饱和区 p↑ -> bias↑（严格）")
    # 注意：必须用「首 token 互不相同」的概念，否则按 token id 去重会合并。
    ps = [0.0005, 0.002, 0.008, 0.03, 0.09]
    concepts = ["想", "念", "记", "忆", "识"]   # 5 个互异单字 -> 互异 token
    dist = dict(zip(concepts, ps))
    out = build_logit_bias(_State(distribution=dist, event_strength=0.0))
    vals = [out.get(str(_first_tid(c))) for c in concepts]
    ok = all(v is not None for v in vals) and all(
        vals[i] < vals[i + 1] for i in range(len(vals) - 1))
    _check("bias 随 p 严格递增", ok, f"bias={vals}")


def test_seed_exclusion():
    print("\n[A5] 种子排除  用户输入已有的种子概念不偏置")
    seed_c = "雨"
    dist = {seed_c: 1.0, "梦": 1.0}   # 两个都满概率
    out = build_logit_bias(_State(distribution=dist, seeds=[seed_c], event_strength=0.0))
    seed_present = str(_first_tid(seed_c)) in out
    other_present = str(_first_tid("梦")) in out
    _check("种子概念被排除", not seed_present, f"种子 token 误出现在输出")
    _check("非种子概念仍偏置", other_present, "非种子概念正常发送")


def test_chosen_boost():
    print("\n[A6] 选中增益  主念 ×CHOSEN_BOOST 且随事件强度饱和放大")
    pc, nc = "念", "默"   # 主念 / 普通，概率相同
    p = 0.04
    es = 60.0             # = EVENT_STRENGTH_REF -> frac=0.5
    st = _State(
        distribution={pc: p, nc: p},
        chosen=[(pc, p)],
        thoughts=[_Thought(concepts=[(pc, p)], is_primary=True)],
        event_strength=es,
    )
    out = build_logit_bias(st)
    bp = out.get(str(_first_tid(pc)))
    bn = out.get(str(_first_tid(nc)))
    exp_primary = _expected_bias(p, boost_factor=config.BIAS_CHOSEN_BOOST, event_strength=es)
    exp_normal = _expected_bias(p, boost_factor=1.0, event_strength=es)
    _check("主念 bias 与公式一致", bp is not None and abs(bp - exp_primary) < 1e-9,
           f"期望={exp_primary} 实际={bp}")
    _check("普通 bias 与公式一致", bn is not None and abs(bn - exp_normal) < 1e-9,
           f"期望={exp_normal} 实际={bn}")
    _check("主念 > 普通（选中更响亮）", bp is not None and bn is not None and bp > bn,
           f"主念={bp} 普通={bn}")


def test_secondary_boost():
    print("\n[A7] 次级增益  副念按 SECONDARY_BIAS_FRACTION 折让（介于主念与普通之间）")
    primary_c, sec_c, norm_c = "甲", "乙", "丙"
    p = 0.04
    es = 60.0
    st = _State(
        distribution={primary_c: p, sec_c: p, norm_c: p},
        chosen=[(primary_c, p)],
        thoughts=[
            _Thought(concepts=[(primary_c, p)], is_primary=True),
            _Thought(concepts=[(sec_c, p)], is_primary=False),
        ],
        event_strength=es,
    )
    out = build_logit_bias(st)
    bp = out.get(str(_first_tid(primary_c)))
    bs = out.get(str(_first_tid(sec_c)))
    bn = out.get(str(_first_tid(norm_c)))
    exp_sec = _expected_bias(
        p, boost_factor=1.0 + (config.BIAS_CHOSEN_BOOST - 1.0) * config.SECONDARY_BIAS_FRACTION,
        event_strength=es)
    _check("副念 bias 与公式一致", bs is not None and abs(bs - exp_sec) < 1e-9,
           f"期望={exp_sec} 实际={bs}")
    _check("主念 > 副念 > 普通",
           bp is not None and bs is not None and bn is not None and bp > bs > bn,
           f"主={bp} 副={bs} 普={bn}")


def test_truncation():
    print("\n[A8] 条目截断  超过 BIAS_MAX_ENTRIES 只保留 bias 最大的 N 条")
    # 取足够多的互异单字（400 个），保证经过 junk 过滤后仍有 >128 个有效 token，
    # 满概率 -> 全部钳到 BIAS_MAX -> 触发截断。
    dist = {chr(0x4E00 + i): 1.0 for i in range(400)}
    out = build_logit_bias(_State(distribution=dist, event_strength=0.0))
    n = len(out)
    _check("输出条目 <= BIAS_MAX_ENTRIES", n <= config.BIAS_MAX_ENTRIES,
           f"n={n} 上限={config.BIAS_MAX_ENTRIES}")
    _check("截断确实发生（有效概念>上限）", n == config.BIAS_MAX_ENTRIES,
           f"截断后 n={n}")


def test_determinism():
    print("\n[A9] 确定性  相同输入 -> 完全相同输出")
    dist = {chr(0x4E00 + i): 0.05 + 0.01 * i for i in range(50)}
    st = _State(distribution=dist, chosen=[(chr(0x4E00), 0.05)],
                thoughts=[_Thought(concepts=[(chr(0x4E00), 0.05)], is_primary=True)],
                event_strength=30.0)
    a = build_logit_bias(st)
    b = build_logit_bias(st)
    _check("两次输出完全一致", a == b, f"len={len(a)}")
    # 所有值都是 2 位小数
    ok_round = all(abs(v - round(v, 2)) < 1e-9 for v in a.values())
    _check("bias 均为 2 位小数", ok_round)


def test_empty_and_switch():
    print("\n[A10] 开关 / 空输入")
    _check("空分布 -> {}", build_logit_bias(_State(distribution={})) == {})
    # 临时关闭总开关
    orig = config.TOKEN_BIAS_ENABLED
    config.TOKEN_BIAS_ENABLED = False
    try:
        out = build_logit_bias(_State(distribution={chr(0x4E00): 1.0}))
        _check("TOKEN_BIAS_ENABLED=False -> {}", out == {})
    finally:
        config.TOKEN_BIAS_ENABLED = orig


# ============================================================
# [B] 在线模型效应测量（可选 --live）
# ============================================================
def _try_openai_client():
    try:
        from openai import OpenAI
    except Exception as e:  # 没装 openai
        return None, f"openai 未安装: {e}"
    # llama-server（logit_bias 真生效）优先，Ollama 后备（已知形同虚设）
    candidates = [
        (config.LLAMA_SERVER_URL + "/v1", "llama-server", "qwen"),
        (config.OLLAMA_URL + "/v1", "ollama", config.OLLAMA_MODEL),
    ]
    import requests
    for base, name, model in candidates:
        try:
            r = requests.get(base.split("/v1")[0] + "/health", timeout=2)
            if r.status_code not in (200, 404):
                continue
        except Exception:
            continue
        try:
            cli = OpenAI(base_url=base, api_key="x", timeout=120)
            # 探活：发一个极小请求
            cli.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}],
                                        max_tokens=1)
            return cli, f"{name}@{base} model={model}"
        except Exception as e:
            last = f"{name}: {e}"
            continue
    return None, "未找到可达的模型服务（llama-server 8081 / Ollama 11434）"


def _token_prob(client, model, prompt, bias_map, top_logprobs=20):
    """发一次请求，返回目标 token 在「第一步生成」的解码概率（exp(logprob)）。"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1,
        temperature=0.0,
        logprobs=True,
        top_logprobs=top_logprobs,
        logit_bias=bias_map,
    )
    lp = resp.choices[0].logprobs
    if not lp or not lp.content or not lp.content[0].top_logprobs:
        return {}
    return {item.token: math.exp(item.logprob) for item in lp.content[0].top_logprobs}


def run_live_test(target: str = "音乐", bias_value: float = 8.0, prompt: str = "现在我想听点"):
    print(f"\n[B] 在线效应测量  target='{target}' bias={bias_value}")
    client, info = _try_openai_client()
    if client is None:
        print(f"  [SKIP] {info}（--live 需要模型服务在跑；离线检验已覆盖公式正确性）")
        return
    print(f"  连接到: {info}")
    tids = qtok.concept_token_ids(target)
    if not tids:
        print(f"  [SKIP] 目标概念 '{target}' 在 qwen 词表里无有效 token")
        return
    tid = tids[0]
    tok = qtok._get().decode([tid])
    model = _model_from_info(info)

    base = _token_prob(client, model, prompt, {})
    biased = _token_prob(client, model, prompt, {str(tid): bias_value})

    pb = base.get(tok, base.get(str(tid), 0.0))
    pbiased = biased.get(tok, biased.get(str(tid), 0.0))
    ratio = (pbiased / pb) if pb > 0 else float("inf")
    print(f"    目标 token='{tok}' (id={tid})")
    print(f"    无偏置时 P={pb:.4f}   偏置={bias_value} 时 P={pbiased:.4f}")
    print(f"    概率放大倍数 ≈ {ratio:.2f}x")
    if ratio > 1.5:
        print("  [EVIDENCE] logit_bias 确实显著抬升了目标 token 的解码概率 —— 端到端生效。")
    elif abs(ratio - 1.0) < 0.05:
        print("  [NOTE] 概率几乎不变 —— 该后端 logit_bias 未生效（与 Ollama 形同虚设一致）。")
    else:
        print("  [WEAK] 有轻微变化，效应偏弱。")


def _model_from_info(info: str):
    # info 形如 "llama-server@http://.../v1 model=qwen"
    if "model=" in info:
        return info.split("model=")[-1]
    return "qwen"


def run_all(live: bool = False, target: str = "音乐", bias: float = 8.0):
    print("=" * 64)
    print("logit_bias 量化验证")
    print("=" * 64)
    if not qtok.available():
        print("[ERROR] qwen 分词器不可用（缺 qwen2.5_tokenizer.json），无法运行。")
        return 2

    test_formula_basic()
    test_clamp_max()
    test_floor()
    test_monotonic()
    test_seed_exclusion()
    test_chosen_boost()
    test_secondary_boost()
    test_truncation()
    test_determinism()
    test_empty_and_switch()

    if live:
        run_live_test(target=target, bias_value=bias)

    print("\n" + "=" * 64)
    print(f"离线检验: {len(_PASSED)} 通过 / {len(_FAILED)} 失败")
    if _FAILED:
        print("失败项: " + ", ".join(_FAILED))
    print("=" * 64)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外跑在线模型效应测量")
    ap.add_argument("--target", default="音乐", help="在线测量目标概念")
    ap.add_argument("--bias", type=float, default=config.BIAS_MAX, help="在线测量施加的 bias 值")
    args = ap.parse_args()
    sys.exit(run_all(live=args.live, target=args.target, bias=args.bias))
