# logit_bias 量化验证

> 对应需求："logit_bias 缺少量化验证"。
> 本文是 `src/clayer/test_logit_bias.py` 的设计说明与实测结论，作为意识层「最后一公里」的可验证性闭环。

---

## 1. 背景与缺口

意识层六步流水线（`consciousness.py`）的最后一步是 **assemble**：把"这一念"交给 LLM。
其中 **token 级介入** `token_bias.build_logit_bias(state)` 把意识层算出的概率分布编译成
`{token_id: bias}`，直接加在 qwen 每一步解码的 logits 上（softmax 之前）。

这是"提示词软引导"下沉到"**解码硬介入**"的关键落点：
意识层的统计 + 链式 + 概率结构，直接调制 transformer 的每一步解码——
权重占比大的概念，其 token 在每一步被抬得越高，被"调用并组合"进语言输出的概率就越大。

**此前这条链路是验证盲区**：

- `AssocGraph` / `MemoryStore` 有 C++ `parity_test`（数值对照，容差 1e-9）；
- `token_bias` 是**纯 Python 且零测试**：
  1. 没有公式正确性验证（`bias = GAIN·p^GAMMA` 算得对不对？）
  2. 没有性质验证（单调 / 钳制 / 截断 / 种子排除）
  3. 没有"喂给模型后解码是否真被改变"的**端到端证据**

这就是"logit_bias 缺少量化验证"的缺口所在。

---

## 2. 待验证的对象：公式回顾

```
bias(token) = clamp( GAIN · P(c)^GAMMA , 0, BIAS_MAX )
              再丢弃 < BIAS_MIN_EFFECTIVE 的条目（省带宽）
```

- **选中（主念）概念**额外 `×BIAS_CHOSEN_BOOST`，并随事件强度饱和放大：
  `×(1 + EVENT_BIAS_GAIN · frac)`，其中 `frac = E / (E + EVENT_STRENGTH_REF)`
- **副念概念**折让到 `1 + (BOOST − 1) · SECONDARY_BIAS_FRACTION`
- **种子概念**（用户输入里已有的词）不偏置——模型本来就看得见它们
- 多 token 概念：每个有效 token 都给 bias；同 token 被多个概念命中时取 **max**（不叠加防爆）
- 最终按 bias 从大到小截断到 `BIAS_MAX_ENTRIES` 条，值 `round` 到 2 位小数

标定量（`cl_config.py`）：

| 常量 | 值 | 含义 |
|---|---|---|
| `BIAS_GAIN` | 26.0 | 概率→bias 增益 |
| `BIAS_GAMMA` | 0.5 | <1 压缩差距（弱念也有存在感） |
| `BIAS_MAX` | 8.0 | 单 token 上限（防独裁/复读） |
| `BIAS_MIN_EFFECTIVE` | 0.3 | 低于此值不发送 |
| `BIAS_CHOSEN_BOOST` | 1.6 | 主念额外增益 |
| `SECONDARY_BIAS_FRACTION` | 0.30 | 副念折让比例 |
| `EVENT_BIAS_GAIN` | 0.25 | 事件强度放大增益（饱和上限 1+GAIN） |
| `EVENT_STRENGTH_REF` | 60.0 | 事件强度饱和参考 |
| `BIAS_MAX_ENTRIES` | 128 | 条目上限（工作记忆宽度） |

---

## 3. 两层验证设计

### A. 离线数值 / 性质检验（不依赖模型，确定性，秒级）

文件 `src/clayer/test_logit_bias.py`，**10 组、22 条断言**。
为避免拉起整层，用轻量 `_State` / `_Thought` 替身（只暴露 `build_logit_bias`
实际读取的 `distribution / chosen / seeds / thoughts / event_strength`），
并用镜像公式 `_expected_bias()` 作断言基准：

| 编号 | 验证项 | 关键断言 |
|---|---|---|
| A1 | 公式正确性 | `bias ≈ GAIN·p^GAMMA`，clamp + 地板 |
| A2 | 上限钳制 | 满概率→`BIAS_MAX`；无任何条目超 `BIAS_MAX` |
| A3 | 有效地板 | `< BIAS_MIN_EFFECTIVE` 的概念不发送 |
| A4 | 单调性 | 未饱和区 `p↑ ⇒ bias↑`（严格） |
| A5 | 种子排除 | 用户输入已有的种子概念不偏置 |
| A6 | 选中增益 | 主念 `×1.6` 且随事件强度饱和放大；主念 > 普通 |
| A7 | 次级增益 | 副念按 `SECONDARY_BIAS_FRACTION` 折让；主 > 副 > 普 |
| A8 | 条目截断 | 有效概念 > `BIAS_MAX_ENTRIES` → 截断到 128 条 |
| A9 | 确定性 | 同输入→同输出；均为 2 位小数 |
| A10 | 开关 / 空输入 | `TOKEN_BIAS_ENABLED=False` 或空分布 → `{}` |

> 注意：A4 / A8 必须用「首 token 互异」的概念，否则按 token id 去重会把不同概念合并。

### B. 在线模型效应测量（`--live`，需模型服务在跑）

直接对模型发两次 `chat.completions` 请求（`logprobs=True, temperature=0`）：

- 第一次 `logit_bias = {}`（基线）
- 第二次 `logit_bias = {目标 token: BIAS_MAX}`（施加偏置）

读取目标 token 在「第一步生成」的解码概率 `P = exp(logprob)`，
量化被抬高多少倍：`ratio = P_biased / P_base`。
这是 **"logit_bias 真生效"的端到端证据**。

- 优先 `llama-server@8081`（llama.cpp，logit_bias 真实生效）
- `Ollama@11434` 作后备，但其实测 `logit_bias` 形同虚设
- 该测试同时**复现**"Ollama 的 logit_bias 无效"这一已知结论

---

## 4. 实测结果（2026-07-30，备份副本 venv）

**离线 22/22 断言全部 PASS**。节选：

```
[A4] 单调性   bias=[0.58, 1.16, 2.33, 4.5, 7.8]   （p 依次 0.0005→0.09）
[A6] 选中增益  主念=8.0 > 普通=5.2                （p=0.04, 事件强度=60）
[A7] 次级增益  主=8.0 > 副=6.9 > 普=5.2
[A8] 条目截断  400 概念 → 128 条
```

**调试图**：初版两处 FAIL 是**测试用例自身**的设计缺陷，不是 `build_logit_bias` 的 bug：

- A4 原用 `概0..概4`，首 token 都是 `概`，按 token id 去重合并 → 全读成同一值；
- A8 原用 200 个汉字，仅 121 个通过 junk 过滤，未触发截断。

修正用例（改用互异单字 / 增加到 400 个）后，实现全部通过。

> **结论**：`token_bias` 实现与理论公式**逐位一致**，且所有边界行为
> （钳制 / 地板 / 截断 / 种子排除 / 确定性）正确。

---

## 5. 运行方式

```bash
venv/Scripts/python.exe src/clayer/test_logit_bias.py            # 仅离线检验
venv/Scripts/python.exe src/clayer/test_logit_bias.py --live     # 离线 + 在线效应测量
venv/Scripts/python.exe src/clayer/test_logit_bias.py --target 音乐 --bias 8.0
```

---

## 6. 与意识层其它验证的关系

- `parity_test.py`：负责 **C++ 核心**（`AssocGraph`/`MemoryStore`）与 Python 基准的数值对照（容差 1e-9）。
- 本测试：负责"最后一公里" **`token_bias` 的正确性 + 端到端效应**。

两者互补，共同构成意识层可验证性的闭环。
**建议**：把本测试挂进 `_core.self_test()`，与小念启动时的 C++ parity 自检并列，
使 `logit_bias` 也进入"启动即自检"的保障体系。

---

## 7. 已知边界 / 后续

1. **Ollama 的 `logit_bias` 实测无效**，必须走 `llama-server`（llama.cpp）才能真正调制解码；
   `cl_config.py` 已写明 `LLAMA_SERVER` 优先、`OLLAMA` 仅后备。
2. 当前 `token_bias` 仍是纯 Python（`_core.py` 显式保留，未移植 C++）；无需 parity，
   但本离线检验已等价替代其"正确性保障"。
3. 可选增强：把 `build_logit_bias` 也做 C++ 移植并加入 `parity_test`（与 `AssocGraph` 同法），
   进一步提速"越聊越慢"的长回复动态偏置。
