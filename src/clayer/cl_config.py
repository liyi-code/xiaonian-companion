# -*- coding: utf-8 -*-
"""
全局配置：把"人的意识数学本质"里的各个常量集中在这里。

对应用户理论：
  储存量        -> STORAGE_CAPACITY      (脑容量：信息单元上限)
  基础概率      -> BASE_PROB             (每个被解锁项的最低选取概率，保证弱项也有机会)
  最大概率      -> MAX_PROB              (单个项的最高选取概率，防止某项独裁 -> 避免思维僵化)
  统计->温度    -> TEMP_BASE / TEMP_STAT_BETA (统计量越大分布越尖，偏向越强)
  链式解锁      -> SPREAD_HOPS / SPREAD_DECAY / SPREAD_THRESHOLD
  伪随机组合    -> COMBINE_MIN_K / COMBINE_MAX_K (一次意识里组合多少个概念)
  遗忘          -> DECAY_PER_TURN / FORGET_FLOOR
"""
from __future__ import annotations
import os

# ---------- 感知粒度（输入端：文本 -> 概念） ----------
PERCEPTION_USE_POS = True       # 用 jieba 词性标注滤虚词、按词性给显著度权重（更干净的概念）
PERCEPTION_MIN_WEIGHT = 0.6     # 概念显著度低于此值直接丢弃（滤掉弱信息噪声/量词/方位）
PROXIMITY_WINDOW = 3            # 邻近共现窗口：一句里相隔 <=N 的概念才建"邻近边"
PROXIMITY_DECAY = 0.5           # 邻近边强度随距离衰减：相邻最强，越远越弱
SEED_ENERGY_FROM_WEIGHT = True  # 种子初始激活能量由感知显著度驱动（重要词更用力解锁链式）

# ---------- 链式解锁「强度」维度 ----------
# 强度 S(c) = K * (关联数 + 相似数)，单向正相关：只影响新词，关联/相似词强度不变 -> 单词强度是定值。
# 关联数 = 与 c 共现且边权重达阈值的邻居数；相似数 = 与 c 共享邻居(Jaccard>=阈值)的其他概念数。
STRENGTH_K = 1.0               # 强度标定系数
# STRENGTH_REF 归一化基准对齐人类语义网「平均度数≈13」(Steyvers & Tenenbaum 2005)：
# 取 14 使强度分数刻度贴近真实语义网连接密度（12 -> 14）。
STRENGTH_REF = 14.0            # 强度归一化参考：强度分数 = min(1, S/REF)，喂给解锁能量（渐变不早饱和）
# 动作概念（[ACT_xxx]）注册进意识层时的初始强度。需 >0 才能被 spread_activation 当作候选节点，
# 否则 register_actions 引用的 BASE_STRENGTH 缺失会导致动作注册失败、自发动作永远哑火成 [ACT_IDLE]。
BASE_STRENGTH = 1.0            # 新注册动作节点的初始基础强度（与 STRENGTH_K 同级，给扩散激活一个起点）
STRENGTH_ASSOC_MIN = 0.05      # 关联边权重达到此值才计入"关联数"（滤掉弱噪声边）
SIM_THRESHOLD = 0.25           # 相似度(Jaccard 共享邻居)达到此值才算"相似"
COMPOSE_BONUS = 0.15           # 事件强度：词越多事件越强，每多一词额外 +(n-1)*BONUS 倍

# ---------- 强度「近因衰减」：长期未被调用的概念，强度逐轮降，归零则遗忘 ----------
# 解决"冷门概念永驻图里、思维越积越乱"：一个词若长时间没被调取/激活（think 的扩散激活
# 或 learn 的使用概念会刷新其时间戳），其强度 S(c) 每轮乘 STRENGTH_RECENCY_DECAY 缓慢下滑；
# 闲置超过 STRENGTH_IDLE_GRACE 轮才开始衰减（近期用过的词有宽限期，避免刚学就忘）；
# 降到 STRENGTH_FORGET_THRESHOLD 以下即 drop 遗忘，连带清边。让图自然保持清爽、有界。
STRENGTH_RECENCY_ENABLED = True   # 总开关
STRENGTH_RECENCY_DECAY = 0.992     # 闲置每轮强度乘此系数（越接近1忘得越慢）
STRENGTH_IDLE_GRACE = 8            # 闲置多少轮内不衰减（刚学/刚用过的词宽限期）
STRENGTH_FORGET_THRESHOLD = 0.5    # 强度低于此值即遗忘（约 0；配合离散删除，非逐字归零）

# 解锁优先级分层：信息量增加 >> 强度 >> 最近（严格分层，权重递减保证次序）
INFO_W = 2.0                   # 信息量增加项权重（种子/本轮回填的概念最高）
STRENGTH_W = 1.0               # 强度项权重
RECENCY_W = 0.5                # 最近项权重
INFO_DELTA_REF = 1.0           # 信息增量归一化参考
RECENCY_TAU = 12.0             # 近因衰减时间常数（逻辑轮次）

# 匹配度门控：概念是否被"运用"(解锁)由它与当前上下文(种子)的匹配度决定
MATCH_THRESHOLD = 0.12         # 匹配度低于此值的概念不解锁（不匹配就不运用）
MATCH_ASSOC_W = 0.6            # 匹配度 = 关联匹配*W + 相似匹配*(1-W)，二者是独立指标
EVENT_BIAS_GAIN = 0.25         # 事件强度对"选中念"token bias 的放大增益(饱和上限 1+GAIN)
EVENT_STRENGTH_REF = 60.0       # 事件强度饱和参考：frac = E/(E+REF)，避免数值过大把 bias 顶到上限

# ---------- 动态逐段偏置（长回复中"边说边想"） ----------
# 长回复不一次性定 bias，而是切成若干"段"：每生成一段就重新跑一次意识活动，
# 用最新一段(模型刚说的话)作为新种子重算联想/情绪/注意力，更新该段的 logit_bias +
# 采样温度，再生成下一段 -> 模拟"边说边想"，意识随自身输出流动作。
DYNAMIC_BIAS_ENABLED = True    # 总开关：开启动态逐段偏置
SEGMENT_MAX_TOKENS = 48        # 每段最多生成多少 token（约一句），到量强制切段
SEGMENT_STOP = ["\n", "。", "！", "？", "；"]  # 段边界：遇这些标点即切段并重做意识活动
DYNAMIC_MAX_CHARS = 1400        # 动态模式整体回复长度上限(字符)，防失控
DYNAMIC_MAX_SEGMENTS = 40       # 最多切多少段，防死循环
DYNAMIC_CONTINUE_PROMPT = "（请接着上面自然地继续说，保持话题连贯，不要重复已经说过的内容）"

# ---------- 多念竞争 / 意识流 / 情绪 / 注意力 ----------
# 最多同时并行浮现的"意念/意向"数量（意识流宽度）。每轮从联想分布里抽至多 MAX_THOUGHTS
# 道对比鲜明的候选念，它们并行竞争注意力资源，最终主念(注意力最高)驱动语言输出。
MAX_THOUGHTS = 4               # 最多同时产生的意念/意向数：对齐 Cowan(2001) 注意力焦点容量 4±1（3 -> 4）
THOUGHT_SUPPRESS = 0.85        # 逐代抑制：抽完一道念后，其概念被选概率×(1-SUPPRESS)再抽下一道，保证各念对比鲜明
VIGOR_STRENGTH_W = 1.0         # 念竞争力 vigor：事件强度(饱和 frac)权重
VIGOR_MATCH_W = 0.6            # 念竞争力：与种子的匹配度权重
VIGOR_AROUSAL_W = 0.4          # 念竞争力：情绪唤醒度权重
ATTENTION_TEMP = 0.8           # 注意力分配 softmax 温度(低->注意力越集中主念；高->多念平分)
SECONDARY_BIAS_FRACTION = 0.30 # 次级意念对 token 偏置的贡献比例(0=仅主念最响亮)

# ---------- 动态自注意力（类人：精力有限 -> 注意力必须分配） ----------
# 1) 全局注意力预算：唤醒↑ -> 视野变窄(隧道效应)；疲劳↑ -> 注意力变散变弱
ATTN_NARROW_AROUSAL = 0.45     # 唤醒度→窄化系数：temp 随唤醒收缩的最大比例(0=不窄化)
ATTN_SPREAD_FATIGUE = 0.70     # 疲劳度→发散系数：temp 随疲劳放大的最大比例(0=疲劳不影响)
ATTN_FLOOR = 0.04              # 注意力下限：预算紧张时低于此份额的念被"忽略"(归零再归一)
# 2) 自顶向下(价值/目标) 与 自底向上(刺激显著度) 的显式对抗
ATTN_BOTTOMUP_W = 1.0          # 自底向上力(种子匹配+唤醒)权重；=1 时与旧版行为一致
ATTN_TOPDOWN_W = 1.0           # 自顶向下力(wellbeing 价值对齐)权重；=1 时与旧版行为一致
# 3) 注意力疲劳：每轮 think 消耗精力，随时间恢复；高疲劳 -> 注意力发散 + 想睡提示
ATTN_FATIGUE_PER_THINK = 0.018 # 每轮 think 固定精力消耗
ATTN_FATIGUE_LOAD_K = 0.012    # 刺激负载(候选念的饱和强度)额外消耗系数
ATTN_FATIGUE_RECOVER = 0.04    # 精力恢复速率(每秒指数恢复)
ATTN_FATIGUE_SLEEPY = 0.55     # 疲劳超过此值 -> 注入"想睡"提示(接入现有睡眠机制)

# ---------- 底层价值逻辑：让使用者的生活越来越好 ----------
# 元目标(meta-goal)：意识层的最终判断标准。多念竞争里，更契合"让使用者生活越来越好"
# 的意念获得更高的价值分(wellbeing)，叠加进竞争力 vigor 的第四轴，使主念(对外输出)
# 本身服务于"助人向善"——即便某念事件强度高/情绪更躁动，若它拉低生活品质也会被压下去。
WELLBEING_ENABLED = True        # 总开关：关闭则价值分不进入竞争
WELLBEING_W = 0.9               # 价值分对竞争力 vigor 的权重（与强度/匹配/唤醒并列的第四轴）
# 促进生活向好的方向（成长/健康/秩序/关系/积极行动）
WELLBEING_POSITIVE = {
    "成长", "进步", "学习", "阅读", "运动", "健康", "早睡", "早起", "休息", "规划",
    "目标", "计划", "习惯", "自律", "整理", "清洁", "沟通", "陪伴", "朋友", "家人",
    "努力", "坚持", "行动", "解决", "改善", "希望", "开心", "治愈", "专注", "复盘",
    "储蓄", "理财", "冥想", "减压", "鼓励", "自信", "勇气", "温柔", "善良", "帮助",
    "创造", "思考", "提问", "记录", "总结", "按时", "规律", "阳光", "微笑", "表达",
    "探索", "尝试", "原谅", "感恩", "耐心", "平和", "充实", "踏实", "清醒", "突破",
}
# 拉低生活品质的方向（消极/放纵/内耗/伤害）
WELLBEING_NEGATIVE = {
    "熬夜", "摆烂", "放纵", "暴食", "暴饮", "拖延", "焦虑", "自暴自弃", "逃避",
    "沉迷", "酗酒", "吸烟", "赌博", "懒惰", "消极", "抑郁", "内耗", "浪费", "冲动",
    "愤怒", "绝望", "孤独", "委屈", "生病", "危险", "崩溃", "放弃", "抱怨", "指责",
    "仇恨", "伤害", "自残", "孤立", "内卷", "颓废", "消沉", "莽撞", "短视", "敷衍",
}

# 情绪维度（效价-唤醒 valence-arousal 二维模型）
MOOD_VALENCE_INIT = 0.12       # 全局情绪初始效价[-1,1]（健康个体基线情感略偏正 positivity offset, Diener）（0.10 -> 0.12）
MOOD_AROUSAL_INIT = 0.30       # 全局情绪初始唤醒[0,1]
MOOD_INERTIA = 0.85            # 全局情绪惯性（越大越稳、飘得慢）
EMOTION_ENTROPY_AROUSAL_W = 0.45   # 熵对唤醒贡献：意识越发散越"躁动"
EMOTION_STRENGTH_AROUSAL_W = 0.45  # 事件强度对唤醒贡献：事件越强越"激动"
EMOTION_LEXICON_AROUSAL_W = 0.40  # 情绪词库命中对唤醒贡献

# ---------- 储存量（脑容量） + 关联图容量（防爆内存 / 防意大利面） ----------
STORAGE_CAPACITY = 42000        # 最多记住多少个信息单元（概念）：对齐成人平均词元量 ~42000（Brysbaert 2016）（20000 -> 42000）
EDGE_CAPACITY_PER_NODE = 96     # 每个概念最多保留多少条关联边（突触上限）：语义网无标度枢纽需更高连接度（64 -> 96）
# 关联图「全局节点数」硬上限：超过则按活跃度(总边权)从低到高淘汰最不活跃的概念及其边。
# 真实分词后节点多是"词"，量级在数万，此上限是双保险；设 0 表示不限制。
# 这直接回答"怕爆内存/思维变乱"——图永远有界、稀疏、自动遗忘冷门概念。
GRAPH_NODE_CAPACITY = 60000
# 每轮遗忘衰减系数（越接近 1 忘得越慢）见下方"统计/学习/遗忘"段的 DECAY_PER_TURN。

# ---------- 睡眠 / 记忆压缩整合（性能触发的自愈保险 · 第 5 道遗忘保险） ----------
# 当每轮对话"遍历记忆图"的耗时逼近/超过阈值，模型进入睡眠态：
#  · 逼近 SLEEP_WARN_MS      -> 犯困态：自动向用户表达"好累、想睡"（仍正常对话）
#  · 超过 SLEEP_FORCE_MS      -> 强制睡眠：输出"我太累了，睡觉了"并停止输出，随即压缩整合记忆
#  · 压缩整合目标由强度决定：强度快要归零的弱词优先并入其最强相关词（汇入相关新词）
#  · 用户也可手动强制睡眠（GUI 按钮 / 工具）
#  · 若压缩后遍历耗时仍超阈值 -> 进入"用户筛选存储"模式（删词库 / 备份另存 / 筛选另存）
SLEEP_WARN_MS = 450.0              # 遍历耗时逼近阈值(ms) -> 犯困态，向用户表达想睡
SLEEP_FORCE_MS = 500.0             # 遍历耗时超过(ms) -> 强制睡眠：停输出 + 压缩整合
SLEEP_RECOVER_MS = 360.0           # 耗时回落到此以下才解除犯困/睡眠态
SLEEP_COST_K = 1.0                 # benchmark_ms 标定系数(经验；越大越早触发，可调)
CONSOLIDATE_STRENGTH_MAX = 2.5     # 强度低于此值(快要归零)的弱词优先并入最强相关词
CONSOLIDATE_BATCH = 200            # 单次强制睡眠最多合并的弱词数(防一次卡死)
SLEEP_CONSOLIDATE_MAX_STEPS = 30   # 压缩整合最多迭代轮数
SLEEP_FORCED_PHRASE = "呼……我有点转不动啦，先去睡一会儿咯～你也早点休息，别熬太晚哦💤"
SLEEP_SCREENING_PROMPT = ("记忆太多啦，我整理不动了。你可以：① 让我把词库备份另存一份；"
                          "② 直接清空词库重来；③ 挑一些想保留的词筛选另存。你想怎么处理呀？")

# ---------- 双通路睡眠（睡眠时的两种记忆处理：语义压缩 + 创新组合，模拟想象力/创造力雏形） ----------
# 通路一 = 既有 consolidate()（弱词并入强词的语义压缩，降遍历耗时）。
# 通路二 = 创新组合：睡眠时把当前高权重节点（strength_score 超阈值的"活跃概念"）随机两两
#          组合成"合成概念"，新权重 = 组合成员权重之和；合成概念存独立索引（不侵入主关联图，
#          可一键回退），并可与其他合成概念/原概念建立弱连接（模拟"联想碰撞"）。
# 表层（扩散激活）思索不出答案时，可回查合成概念索引作为"想象力出口"。
DUAL_PATHWAY_ENABLED = True         # 总开关：False 则只跑通路一（行为与旧版完全一致）
DUAL_COMBINE_THRESHOLD = 0.8        # 通路二只组合 strength_score 超过此值的高权重节点（起步设高，驯化期保守）
DUAL_COMBINE_MIN_K = 2              # 每次组合最少概念数
DUAL_COMBINE_MAX_K = 2              # 每次组合最多概念数（起步设 2，驯化期先只做二元组合）
DUAL_COMBINE_BATCH = 12             # 单次睡眠最多生成多少个合成概念（防爆炸）
DUAL_COMBO_CAPACITY = 400           # 合成概念索引容量上限（超出按 slope_utility 从低淘汰）
DUAL_UTILITY_WINDOW = 6             # slope_utility 记录的最近 N 次调用斜率窗口
DUAL_UTILITY_MIN = -1.0             # slope_utility 下限（负 = 该组合在当前情境有害，检索时丢弃）
DUAL_WEAK_LINK = 0.25               # 合成概念之间的弱连接初始权重
DUAL_RETRIEVE_THRESHOLD = 0.5       # 表层卡壳时，slope_utility 低于此值的合成概念不返回

# ---------- 概率：基础概率 & 最大概率 ----------
BASE_PROB = 0.02                # 基础概率：任何被解锁的概念至少有这么大机会被选中（随机性/灵感）
MAX_PROB = 0.55                 # 最大概率：任何单个概念的选取概率上限（防独裁/防僵化）

# ---------- 统计数量 -> 偏向概率波动（温度调制） ----------
TEMP_BASE = 1.30                # 初始温度（统计很少时，分布平、偏向弱、随机波动大）
TEMP_STAT_BETA = 0.20           # 统计量对温度的收敛强度；越大，见得越多分布越尖锐（统计学习加速 0.18 -> 0.20）
TEMP_MIN = 0.35                 # 温度下限（永远保留一点点波动，不会完全确定）

# ---------- 链式解锁（扩散激活） ----------
SPREAD_HOPS = 4                 # 最多沿关联链解锁几跳：人类语义网小世界平均路径 ~3-4（Steyvers & Tenenbaum 2005）（3 -> 4）
SPREAD_DECAY = 0.62            # 每多一跳，激活能量乘的衰减系数：扩散激活可达更远关联（Collins & Loftus 1975）（0.55 -> 0.62）
SPREAD_THRESHOLD = 0.02        # 激活能量低于此值就不再往下解锁（意识边界）
SPREAD_MAX_UNLOCK = 64         # 单次链式解锁最多解锁多少个概念：被激活的长时记忆集大于注意力焦点（48 -> 64）

# ---------- 伪随机组合 ----------
COMBINE_MIN_K = 4              # 一次意识内容至少组合几个概念：对齐 Cowan 4±1 下界（3 -> 4）
COMBINE_MAX_K = 9             # 最多组合几个概念（对齐 Miller 7±2 上界）
COMBINE_FOCUS = 0.5           # 专注度[0,1]：越大越倾向少而精，越小越发散（影响实际 K）

# ---------- 统计 / 学习 / 遗忘 ----------
REINFORCE_NODE = 1.0          # 一个概念被激活/使用，计数加多少
REINFORCE_EDGE = 1.0          # 两个概念共现，边共现计数加多少
DECAY_PER_TURN = 0.997        # 每轮全局衰减系数（缓慢遗忘）
FORGET_FLOOR = 0.15          # 计数衰减到此值以下且容量吃紧时可被淘汰

# ---------- 概率分布 -> 驱动 LLM 采样参数 ----------
# 意识层分布的"熵"决定 qwen 的采样：熵高(发散)->温度高，熵低(专注)->温度低
LLM_TEMP_MIN = 0.30
LLM_TEMP_MAX = 1.10
LLM_TOPP_MIN = 0.70
LLM_TOPP_MAX = 0.98

# ---------- token 级介入（logit_bias 直接调制解码） ----------
TOKEN_BIAS_ENABLED = True      # 总开关：意识层概率分布 -> 每步解码 logits 偏置
BIAS_GAIN = 26.0               # 概率->bias 的增益：bias = GAIN * p^GAMMA
BIAS_GAMMA = 0.5               # <1 压缩差距(弱念也有存在感)；>1 放大强者
BIAS_MAX = 8.0                 # 单 token bias 上限（对应"最大概率"思想，防独裁/防复读）
BIAS_MIN_EFFECTIVE = 0.3       # 低于此值的 bias 不发送（省带宽，弱到无意义）
BIAS_CHOSEN_BOOST = 1.6        # 被伪随机组合选中的"这一念"额外增益（当下念头最响亮）
BIAS_MAX_ENTRIES = 128         # logit_bias 条目上限（工作记忆宽度）：配合扩大后的激活集（96 -> 128）

# ---------- LLM 后端 ----------
# 首选 llama.cpp 的 llama-server（logit_bias 真实生效，token 级介入的关键）。
# 它直接加载 ollama 已下载的 GGUF blob，权重零拷贝复用。
# ollama 仅作后备（其 logit_bias 实测形同虚设）。
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:8081")
LLAMA_SERVER_EXE = r"d:\AI训练\llama.cpp\llama-server.exe"
QWEN_GGUF_PATH = r"D:\ollama\blobs\sha256-2049f5674b1e92b4464e5729975c9689fcfbf0b0e4443ccf10b5339f370f9a54"
LLAMA_SERVER_AUTOSTART = True   # run.py 发现 8081 没起时自动拉起 llama-server
LLAMA_SERVER_ARGS = ["-ngl", "99", "-c", "8192", "--port", "8081"]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_TIMEOUT = 600
OLLAMA_NUM_CTX = 8192

# ---------- 持久化 ----------
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_state")
STATE_FILE = os.path.join(STATE_DIR, "mind.json")
# 合成概念索引持久化文件（独立于 mind.json，避免破坏 C++/Python parity）
DUAL_COMBO_FILE = os.path.join(STATE_DIR, "combos.json")

# ---------- 主动找话题（从睡眠成果里挑种子，绝不随机抓网） ----------
# 触发三条件由上层（gui 的 _proactive_tick）判断：① 用户连续空闲 ≥ IDLE 秒；② 状态非
# 勿扰/专注；③ 距上次主动找话题 ≥ 限频间隔。满足才调 idle_topic() 取种子，全程纯规则、
# 不调用 LLM（用户回复后才启动 LLM 续聊）。
TOPIC_ENABLED = True                 # 总开关
TOPIC_IDLE_SECONDS = 5 * 60          # 条件一：连续空闲 5 分钟
TOPIC_COOLDOWN_SECONDS = 2 * 3600    # 条件三：距上次主动找话题 ≥ 2 小时（防骚扰）
# 来源A（创新组合）：combos 里"还没被用过(use_count==0) 且 slope_utility 达标"的合成概念
TOPIC_SOURCE_A_MIN_UTILITY = 0.15    # 合成概念 slope_utility 下限（太低说明组合无价值）
TOPIC_SOURCE_A_WEIGHT_MIN = 0.8      # 合成概念权重下限（太弱的组合不值得拿出来）
# 来源B（遗忘预警）：assoc_graph 里"正在快速衰减(闲置>grace) 但曾高频(关联数大)"的词
TOPIC_SOURCE_B_GRACE = 4             # 闲置轮次超过此值视为"正在衰减"（复用近因遗忘的 grace 语义）
TOPIC_SOURCE_B_ASSOC_MIN = 2         # 关联数下限（曾高频的代理：关联的概念数够多）
# 反馈闭环（"越来越"原则）
TOPIC_FEEDBACK_POSITIVE = 0.5        # 正反馈（用户乐意聊）给种子的加权增量
TOPIC_FEEDBACK_NEGATIVE = -1.0       # 负反馈（无视/骂）拉黑该种子
TOPIC_COOLDOWN_SHRINK_ON_NEGATIVE = 0.5  # 负反馈后主动间隔缩短倍率（"知错就改"）

# 主动找话题话术模板（纯规则，{seed} / {other} 由引擎填充；绝不调用 LLM）
TOPIC_TEMPLATES = [
    "诶，我突然想到，你说过「{seed}」，会不会其实和「{other}」有点关系？",
    "对了，「{seed}」这事，我还一直惦记着呢，后来怎么样了？",
    "我刚刚整理记忆的时候，突然把「{seed}」和「{other}」连起来了，你有这种感觉吗？",
    "说起来，「{seed}」——我好像快把它忘了，趁还记得，想再听你讲讲。",
]
# 睡眠机制的备份/筛选另存目录（STATE_DIR 已定义，放在其后）
SLEEP_BACKUP_DIR = os.path.join(STATE_DIR, "backups")   # 备份另存目录
SLEEP_EXPORT_DIR = os.path.join(STATE_DIR, "exports")   # 筛选另存目录

# ---------- 随机种子（伪随机） ----------
# None = 每次运行不同；设成整数可复现同一段"意识流"
PRNG_SEED = None
