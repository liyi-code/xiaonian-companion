import json
import re
import threading
import time
from openai import OpenAI
from config import CONFIG
from memory import Memory
import tools as _tools  # 动态工具库：实时 _schemas()，自定义行为新增后立即可用
from launcher import launcher
try:
    from transport.registry import send_message as _account_send_message
except Exception:  # pragma: no cover
    _account_send_message = None

# --------------------------------------------------------------------------- #
# 意识层（魔改 qwen2.5）接入：把"多念竞争 + 让生活越来越好"的底层逻辑接到小念的对话上。
# 放在 src/clayer/ 子目录，其 config 已重命名为 cl_config 避免与本项目 config.py 冲突。
# 任何环节异常都自动降级——小念仍按原逻辑运行，不会崩。
# --------------------------------------------------------------------------- #
import sys as _sys
import os as _os

_CONSCIOUSNESS_OK = False
_Consciousness = None
_cl_config = None
_cl_perception = None
_cl_token_bias = None
try:
    _CLAYER_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "clayer")
    if _CLAYER_DIR not in _sys.path:
        _sys.path.insert(0, _CLAYER_DIR)
    import cl_config as _cl_config          # 意识层配置（重命名以避免冲突）
    from consciousness import Consciousness as _Consciousness
    import perception as _cl_perception
    import token_bias as _cl_token_bias
    _CONSCIOUSNESS_OK = True
except Exception as _ce:  # pragma: no cover
    # 意识层不可用（缺依赖/文件），小念照常工作，仅不加载该能力
    _CONSCIOUSNESS_OK = False
    _Consciousness = None
    print(f"[意识层] 未启用（{_ce}）；小念按原逻辑运行。")


class _StreamFilter:
    """增量文本过滤器：在流式输出过程中实时把可见片段交给 on_token 回调，

    同时跳过 <think>...</think> 推理段（deepseek-r1 等会输出；qwen 不输出，过滤零开销）。
    跨 chunk 的标签截断用前缀缓冲处理，保证正常文本不被吞掉。
    """

    TAG_OPEN = "<think>"
    TAG_CLOSE = "</think>"

    def __init__(self, on_token):
        self.on_token = on_token
        self.raw = []
        self._buf = ""
        self._in_think = False

    def feed(self, piece):
        if not piece:
            return
        self.raw.append(piece)
        self._buf += piece
        out = []
        while True:
            if self._in_think:
                i = self._buf.find(self.TAG_CLOSE)
                if i == -1:
                    self._buf = self._keep_prefix(self.TAG_CLOSE)
                    break
                self._in_think = False
                self._buf = self._buf[i + len(self.TAG_CLOSE):]
            else:
                i = self._buf.find(self.TAG_OPEN)
                if i == -1:
                    keep = self._keep_prefix(self.TAG_OPEN)
                    if keep:
                        out.append(self._buf[:-len(keep)])
                        self._buf = keep
                    else:
                        out.append(self._buf)
                        self._buf = ""
                    break
                out.append(self._buf[:i])
                self._in_think = True
                self._buf = self._buf[i + len(self.TAG_OPEN):]
        visible = "".join(out)
        if visible and self.on_token:
            self.on_token(visible)

    def _keep_prefix(self, tag):
        """截断时只保留可能作为 tag 前缀的末尾字符，避免吞掉正常文本。"""
        for k in range(len(tag) - 1, 0, -1):
            if self._buf.endswith(tag[:k]):
                return self._buf[-k:]
        return ""

    def finish(self):
        """流结束：输出剩余非 think 缓冲；仍卡在 think 内的丢弃。"""
        if self._buf and not self._in_think and self.on_token:
            self.on_token(self._buf)
        self._buf = ""


# 本地 Ollama 提速：Ollama 的 OpenAI 兼容 /v1/chat/completions 端点【忽略】keep_alive/num_ctx
# 顶层字段（实测发 keep_alive 仍为默认 5 分钟卸载）。必须用原生 /api/chat 端点才生效。
# 因此改为「后台保活线程 + 启动预热」：周期性用原生端点发 1-token 请求并带 keep_alive=-1，
# 把模型钉在显存里（ollama ps 显示 Forever），彻底消除「每次对话重新加载 9GB 模型」的数秒卡顿
# （这是「没记忆也回复慢」的头号原因）。仅对本地 Ollama 地址启用，云端后端不触发。
# 本地 Ollama 对“同一模型”的请求是串行处理的；若并发发起（预热加载 + 对话生成 + 保活 ping）
# 其内部队列会卡死（表现：请求挂起 300s 无响应，对话干等）。用一把全局锁把所有 Ollama 请求
# 串行化，彻底消除并发导致的卡死。仅对本地 Ollama 生效，云端后端不受影响。
_ollama_lock = threading.Lock()

# 用户正在对话时为 True：后台自主/看屏/关心等生成会据此让位，避免抢对话的 Ollama 槽位。
_user_chat_active = False


def _backend_kind() -> str:
    """后端类型识别：ollama | llama_server | cloud。
    - ollama      : 本地 Ollama（默认 11434 端口），忽略 logit_bias，需原生 /api 保活
    - llama_server: llama.cpp 的 llama-server（OpenAI 兼容，默认 8081 端口），
                   真正支持 token 级 logit_bias，且 CUDA 构建可用 -ngl 全 GPU 加速
    - cloud       : 云端 OpenAI 兼容服务
    """
    base = str(CONFIG.get("base_url", "")).lower()
    if "11434" in base:
        return "ollama"
    if "8081" in base or "llama-server" in base or ("llama" in base and "openai.com" not in base):
        return "llama_server"
    return "cloud"


def _is_local_ollama() -> bool:
    """兼容旧调用：仅当后端是本地 Ollama 为 True（保活 / 原生端点专用）。"""
    return _backend_kind() == "ollama"


def _is_local_backend() -> bool:
    """本地后端（Ollama / llama-server）对同模型请求串行处理，需串行锁防并发卡死。"""
    return _backend_kind() in ("ollama", "llama_server")


def _ollama_native_url() -> str:
    """从 base_url(http://...:11434/v1) 反推原生 /api/chat 地址。"""
    base = str(CONFIG.get("base_url", ""))
    root = base[:-len("/v1")] if base.endswith("/v1") else base.rstrip("/")
    return root + "/api/chat"


def _ollama_keepalive_once(model: str) -> None:
    """用原生端点 ping 一次，keep_alive=-1 让模型常驻显存。失败静默（Ollama 未起也不影响）。"""
    if not model:
        return
    import json as _json
    import urllib.request as _ureq
    try:
        payload = _json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "keep_alive": -1,
        }).encode("utf-8")
        req = _ureq.Request(_ollama_native_url(), data=payload,
                            headers={"Content-Type": "application/json"})
        # 本地保活不走系统代理（避免 Clash 等把 localhost 也代理导致连不上）
        opener = _ureq.build_opener(_ureq.ProxyHandler({}))
        with _ollama_lock:
            opener.open(req, timeout=30).read()
    except Exception:
        pass


def start_ollama_keepalive(model: str, interval_sec: int = 90) -> None:
    """后台线程：每 interval_sec 秒用原生端点保活一次，使对话模型常驻显存（< Ollama 默认 5 分钟）。

    只保活对话模型：视觉模型(llama3.2-vision ~7.8GB)与对话模型(9GB)合计超 16G 显存，
    若同时钉住会互相把对方挤到内存、反复重载；视觉模型按需加载（首次看屏时一次性加载即可）。
    """
    if _backend_kind() != "ollama":
        return
    import threading as _th
    import time as _t
    def _loop():
        while True:
            _ollama_keepalive_once(model)
            _t.sleep(interval_sec)
    _th.Thread(target=_loop, daemon=True).start()


def _default_messenger_send(app, contact, message):
    """没有任何账号接入时的兜底，避免调用方崩溃。"""
    return (False, "小念还没有连接任何账号（QQ/微信未启用），无法代为发送。")


class Session:
    """一次独立对话的状态：私有记忆 + 待发送状态 + 线程锁。

    桌面窗口、QQ、微信各自是不同渠道；同一渠道下不同用户也要互不串台，
    因此每个 (平台, 用户) 用独立的 Session 承载记忆与“待补全发消息”状态。
    """

    def __init__(self, memory, is_owner=False):
        self.memory = memory
        self.is_owner = is_owner
        self.pending = None        # (app, contact|None, message|None)
        self.lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 确定性意图路由：把“打开/启动软件、打开网址、搜索文件、查系统状态”这类
# 明确动作直接从客户端执行，不依赖模型是否“愿意”调用工具 —— 这是
# “说打开就真打开、说搜就真搜”的关键。deepseek-chat 在 tool_choice=auto
# 下经常“说说而已不调工具”，所以动作必须客户端兜底层做掉。
# --------------------------------------------------------------------------- #
_URL_HINT = re.compile(r"https?://|\.(com|cn|net|org|io|gov|edu)(\s|$)", re.I)

# 常见网站别名，方便自然语言直接打开网页
_SITE_ALIAS = {
    "百度": "https://www.baidu.com",
    "谷歌": "https://www.google.com",
    "bing": "https://www.bing.com",
    "必应": "https://www.bing.com",
    "知乎": "https://www.zhihu.com",
    "微博": "https://weibo.com",
    "淘宝": "https://www.taobao.com",
    "京东": "https://www.jd.com",
    "哔哩哔哩": "https://www.bilibili.com",
    "b站": "https://www.bilibili.com",
    "github": "https://github.com",
    "youtube": "https://www.youtube.com",
}

_OPEN_RE = re.compile(
    r"^(?:帮(?:我|忙)?\s*)?"
    r"(打开|启动|运行|开一下|开个|开|launch|open|运行一下)\s*"
    r"(.+)$",
    re.IGNORECASE,
)

# 文件搜索：仅当明确提及"文件/文档/资料"等关键词时触发
# 支持格式："查找文件 X""搜索文件 X""找文件 X"或"搜索 X 文件"
_SEARCH_RE = re.compile(
    r"^(?:帮(?:我|忙)?\s*)?"
    r"(?:搜(?:索)?|查找|找|locate|find|search)\s*"
    r"(?:一?[下个]\s*)?"
    r"(?:文件|文档|资料|文件夹|目录|图片|照片)?(?:\s*)?"
    r"(.+?)"
    r"\s*$",
    re.IGNORECASE,
)


# "查看时间/日期"意图：小念自主掌握当前时间（日期/时刻/星期/时段），
# 便于提醒作息、判断早晚、更自然关心用户。
_TIME_RE = re.compile(
    r"(现在|当前|今天|此刻)?\s*(几点|几时|什么时间|什么时候|几点钟|几点[啦了吧])"
    r"|(今天是|现在是|今天|现在)\s*(几月几日|几号|星期几|周几|礼拜几|什么日期|什么日子)"
    r"|(看|查|问)?\s*(现在|当前|今天)?\s*(几点了|几点|时间|日期)(?:了|呢|呀)?\s*[?？]?$"
    r"|看看?(现在|当前)?(几点了|几点|时间|日期)",
    re.IGNORECASE,
)

# "定时提醒"意图：解析「X分钟后提醒我/叫我/喊我 Y」。
# 组 delay=数值/中文数字，组 unit=时间单位(分钟/小时/秒/分)，组 msg=提醒内容。
# 真正的计时由 GUI 的 on_tool 用 root.after 执行。
_REMINDER_RE = re.compile(
    r"(?P<delay>(\d+(?:\.\d+)?)|(?:一刻钟|一刻|半小时|半|一|两|二|三|四|五|六|七|八|九|十"
    r"(?:[一二三四五六七八九]|五|十)?|二十分钟))\s*"
    r"(?P<unit>分钟|分|小时|个?小时|小时?|秒|秒钟)?\s*"
    r"(?:之后|过后|以后|后|到点了)?\s*"
    r"(?:提醒我|叫我|叫醒我|喊我|喊醒我|提醒|叫|喊|通知|记得提醒)?\s*"
    r"(?:我)?\s*"
    r"(?P<msg>[^。.!！?？\s].*?)\s*[。.!！?？]?\s*$",
    re.IGNORECASE,
)

# 中文数字（不含单位）→ 数值（换算成分钟时再乘单位）
_CN_NUM = {
    "半": 0.5, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
    "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30,
    "四十": 40, "五十": 50, "六十": 60,
    "一刻": 15,
}
# 含单位整体词 → 直接是分钟数（"一刻钟/半小时/二十分钟"）
_CN_NUM_FULL_MIN = {"一刻钟": 15, "半小时": 30, "二十分钟": 20}
# 单位 → 换算成分钟（当 delay 是纯数字、需配单位时）
_UNIT_TO_MIN = {"秒": 1 / 60, "秒钟": 1 / 60, "s": 1 / 60}


def _parse_delay_min(delay, unit=""):
    """把 'X + 单位' 的延迟解析成分钟数（float）。hour→×60, 秒→÷60。"""
    if not delay:
        return None
    d = delay.strip().lower()
    if d in _CN_NUM_FULL_MIN:
        return float(_CN_NUM_FULL_MIN[d])
    if d in _CN_NUM:
        num = _CN_NUM[d]
    else:
        try:
            num = float(d)
        except Exception:
            return None
    u = (unit or "").strip().lower()
    if "小时" in u or "时" in u:
        return num * 60.0
    if u in _UNIT_TO_MIN:
        return num * _UNIT_TO_MIN[u]
    return float(num)  # 默认分钟


_STATUS_RE = re.compile(
    r"(系统状态|电脑状态|系统信息|电脑配置|内存占用|cpu占用|配置信息|"
    r"电脑怎么样|电脑状态|查看配置|系统情况)",
    re.IGNORECASE,
)

# “看屏幕”意图：需要真正看画面才能回答的请求，确定性触发多模态看屏
_SCREEN_RE = re.compile(
    r"(看(?:一?[下看])?\s*(?:我的?|一下)?\s*屏幕|屏幕上(?:是|有)什么|"
    r"看看我在(?:干嘛|干什么|做什么|玩什么)|看(?:一?下)?我这(?:局|把)|"
    r"帮我看看(?:这个)?(?:报错|画面|截图|屏幕)|你看得到(?:我的)?屏幕吗|你能看到屏幕吗)",
    re.IGNORECASE,
)

# 自主权限开关 / 透明查询：让“交给你/你自己看着调/关掉自主/你都改了什么”这类意图
# 确定性落到对应工具，而不是交给模型自由发挥（避免误判）。
_AUTONOMY_ON_RE = re.compile(
    r"(你自己看着调|你帮我盯着|交给你(了)?|你自己决定|你安排|你看着办|"
    r"你说了算|让你自主|开(?:启|放)?自主|自主调)",
    re.IGNORECASE,
)
_AUTONOMY_OFF_RE = re.compile(
    r"(关(?:掉|闭)?自主|别自己改|听我的|你别擅自|收回自主|关了自主)",
    re.IGNORECASE,
)
_AUTONOMY_REVIEW_RE = re.compile(
    r"(你(都|到底)?(改|动|调)了(我)?(什么|哪些|啥)|你(动|改)了我(的)?设置(吗|没)?|"
    r"查看?.*自主.*改动|自主.*(改了|动了)什么)",
    re.IGNORECASE,
)

# “让小念去睡 / 也去休息 / 关机退出”意图：触发【睡眠机制】（记忆整合压缩）+ 优雅关闭程序。
# 关键：单纯的“晚安/安啦”不算——必须明确让她也去睡/休息/关机，避免用户只是道晚安就被关掉。
_SLEEP_RE = re.compile(
    r"(你也|你|小念|念)\s*(?:也\s*)?(?:早点|先|快|去)?\s*(睡|睡觉|休息|歇|眯一会)"
    r"|晚安.{0,12}?(你也|一起|陪我).{0,8}?(睡|休息|歇)"
    r"|去睡(吧|觉)?|睡觉吧|你也歇着|你也早点歇"
    r"|关机|关闭程序|退出程序|退出小念|关掉小念"
    r"|good\s*night|shut\s*down|bye\s*(?:baby|girl)?",
    re.IGNORECASE,
)
# 否定护栏：含“别/不要/不想/不准”等否定词时，不触发睡眠（避免“你别睡/我不想让你睡”误关机）
_SLEEP_NEG_RE = re.compile(
    r"(别|不要|不想|不用|别让|不准|拒绝|算了).{0,8}?(睡|休息|歇|关机|退出|关闭)",
    re.IGNORECASE,
)

# 用户设置状态：让用户用自然语言切到"勿扰/专注"（小念在此状态不主动说话）。
# 如"让我一个人安静一会儿/先别打扰我/我要专心/别跟我说话(一段时间)"。
_DND_RE = re.compile(
    r"(?:让|给|请)?(?:我|咱们)?(?:一个人)?\s*(?:安静|静一静|静静|冷静)\s*(?:一会儿|一下|会儿)?"
    r"|(?:先)?别(?:来)?(?:打扰|烦|吵|跟我说话|理)我"
    r"|我(?:要|想)(?:一个人|自己)?\s*(?:待|待会|静静|安静|专注|专心)"
    r"|(?:我要)?专心(?:工作|学习|做事)?|开启?勿扰|进入勿扰",
    re.IGNORECASE,
)

# 用户恢复常态：取消勿扰/专注
_DND_OFF_RE = re.compile(
    r"(?:我|咱们)?(?:回来|好了|没事|忙完|说完|回来了|可以)(?:了|啦)?\s*$"
    r"|取消勿扰|结束勿扰|(?:不用|别)?(?:再)?勿扰(?:了)?|继续陪我|可以说话(?:了)?",
    re.IGNORECASE,
)


def _clean(s):
    return re.sub(r"[。，！？\.!?吧呀啊吗呢~～\s]+$", "", s or "").strip()


# Ollama / 本地推理模型（如 deepseek-r1）常把思考过程放在 <think>...</think> 里返回，
# 这段不是小念真正要说的话，必须剥掉，否则会被念出来 / 显示出来。
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text):
    if not text:
        return text
    return _THINK_RE.sub("", text).strip()


# 去除名称/关键词里的中文填充词，避免“这个/那个/我的/一下”被当成查询内容
_FILLER_RE = re.compile(r"^(?:这个|那个|我的|我|你|咱|一?[下个])\s*", re.I)


def _strip_filler(s):
    s = _clean(s)
    return _FILLER_RE.sub("", s).strip()


def _parse_send(text):
    """识别“给微信/QQ 联系人发消息”意图。

    返回 (app, contact, message)；app/contact/message 可能为空（缺哪样哪样空，
    由 chat() 用 pending 状态追问补全）。不是发消息意图则返回 None。
    """
    t = (text or "").strip()
    if not t:
        return None
    # 必须是明确的发消息意图，避免把正常聊天（如“给我说个故事”）误判为发消息。
    # 规则 1：出现“发消息/发信息/发条消息/发个消息/留言/发给”等明确词；
    # 规则 2：出现“给<非代词对象>发/说”结构（如“给张三发”“给张三说”）。
    send_kw = re.search(
        r"发\s*(?:送|条|个|一)?\s*(?:消息|信息)|发个信|留言|发给", t
    )
    give_target = re.search(
        r"给\s*(?!我|你|他|她|咱|咱们|你们|他们|您)\S{1,20}?\s*(?:发|说)", t
    )
    if not (send_kw or give_target):
        return None

    app = "微信" if "微信" in t else None
    if app is None and re.search(r"qq|企鹅|腾讯qq", t, re.I):
        app = "QQ"

    # —— 联系人：多种句式 ——
    contact = ""
    # 1) 给 [app] 的/上的 <name> 发/说/留言 ...
    m = re.search(
        r"给\s*(?:微信|qq|企鹅)?\s*(?:的|上的?)?\s*"
        r"([^\s,，:：。.]+?)\s*(?:发|说|留言|[:：,，]|$)",
        t, re.I,
    )
    if m:
        contact = m.group(1)
    # 2) 发[消息]给 [app] 的/上的 <name>
    if not contact:
        m = re.search(
            r"发(?:送|条|个)?\s*(?:消息|信息)?\s*给\s*"
            r"(?:微信|qq|企鹅)?\s*(?:的|上的?)?\s*"
            r"([^\s,，:：。.]+?)\s*(?:[:：,，]|$)",
            t, re.I,
        )
        if m:
            contact = m.group(1)
    # 3) 在 <name>(里/群/群聊) 发/说/留言 ...（群聊常见说法）
    if not contact:
        m = re.search(
            r"在\s*([^\s,，:：。.]+?)\s*(?:里|群|群聊)?\s*(?:发|说|留言)",
            t, re.I,
        )
        if m:
            contact = m.group(1)

    # —— 消息：在“发[消息][给联系人]：/说：/内容是”之后到结尾 ——
    message = ""
    m = re.search(
        r"(?:发\s*(?:送|条|个)?\s*(?:消息|信息|微信消息|qq消息)?"
        r"\s*(?:给\s*(?:微信|qq|企鹅)?\s*(?:的|上的?)?\s*[^\s,，:：。.]+)?"
        r"\s*[:：,，]?\s*(?:内容是?|内容)?"
        r"|说|留言)\s*[:：,，]?\s*(?:内容是?|内容)?\s*(.*)$",
        t, re.I,
    )
    if m:
        message = m.group(1)

    # 注意：message 是发给联系人的原话，不能像联系人那样用 _clean 去掉语气词
    contact = _clean(contact)
    contact = re.sub(r"^(?:上的?|里|那里|这边)\s*", "", contact)
    contact = re.sub(r"(联系人|好友|朋友|同学|同事)$", "", contact).strip()
    message = message.strip()
    message = re.sub(r"^(?:内容是?|内容|说：|说:)\s*", "", message).strip()

    # 无效联系人（动作词 / App 名 / 仅“群/群里”之类）-> 留空让 pending 追问，
    # 但保留已给出的消息内容
    if (not contact or contact == app or contact.lower() in ("微信", "qq", "企鹅")
            or re.search(r"发|消息|信息|说|留言", contact)
            or contact in ("群", "群里", "群聊", "群里面")):
        return (app, "", message)
    return (app, contact, message)


def _route_action(text):
    """识别明确动作意图，返回 (tool_name, args) 或 None。

    优先级：发消息 > 打开软件/网址 > 搜索文件 > 查询系统状态。
    """
    t = (text or "").strip()
    if not t:
        return None

    # 0.0) 用户状态：进入/退出勿扰（"让我一个人安静一会儿" / "取消勿扰"）
    if _DND_OFF_RE.search(t):
        return ("__user_state__", {"state": "normal"})
    if _DND_RE.search(t):
        return ("__user_state__", {"state": "dnd"})

    # 0) 发消息（最高优先级，避免和“打开”等动作混淆）
    s = _parse_send(t)
    if s:
        app, contact, message = s
        return ("send_message", {"app": app, "contact": contact, "message": message})

    # 1) 打开 / 启动
    m = _OPEN_RE.match(t)
    if m:
        name = _strip_filler(m.group(2))
        name = re.sub(r"(软件|应用|程序|app)$", "", name, flags=re.I).strip()
        if name:
            low = name.lower()
            if low.startswith(("http://", "https://")) or _URL_HINT.search(name):
                url = name if low.startswith("http") else "https://" + name
                return ("open_website", {"url": url})
            if name in _SITE_ALIAS:
                return ("open_website", {"url": _SITE_ALIAS[name]})
            if low in _SITE_ALIAS:
                return ("open_website", {"url": _SITE_ALIAS[low]})
            return ("open_application", {"name": name})

    # 1.8) 网络搜索：以"搜/查/搜索"开头的用户请求，默认走网络搜索。
    #      排除：含"文件/文档/文件夹"→后续 search_files 处理。
    _web = re.match(r"(?:帮我|给我|你)?(?:上网\s*)?(?:搜一下|搜一搜|搜索一下|搜索\s|查一下|查查|搜\s|查\s|搜|查)(.+?)",
                    t, re.I)
    if _web and not re.search(r"(?:文件|文档|文件夹|目录)", t, re.I):
        return ("web_search", {"query": _web.group(1).strip()[:100]})

    # 2) 搜索 / 查找文件（仅在明确说"搜索文件/查找文件/搜文件"时触发）
    m = _SEARCH_RE.match(t)
    if m:
        pattern = _strip_filler(m.group(1))
        if pattern:
            return ("search_files", {"pattern": pattern})

    # 3) 查询系统状态
    if _STATUS_RE.search(t):
        return ("get_system_status", {})

    # 3.5) 查看时间/日期（小念自主掌握当前时间）
    if _TIME_RE.search(t):
        return ("get_current_time", {})

    # 3.6) 定时提醒：真正设置一个 X 分钟后提醒 Y 的定时器（由 GUI 执行计时与播报）
    m = _REMINDER_RE.match(t)
    if m:
        delay = _parse_delay_min(m.group("delay"), m.group("unit"))
        if delay and delay > 0:
            return ("set_reminder", {"delay_min": delay, "message": m.group("msg").strip()})

    # 3.7) 管理自定义行为（增/删/查小念自己学会的"触发→回应"行为）
    _teach = re.search(
        r"(?:记住|学会|以后|新增|添加|定义|教(?:会)?(?:你|小念)?)[:：，,]?"
        r"[^「『」』]*(?:你|我)?(?:说|讲|问)?[「『](?P<trigger>[^「『」』]{1,40}?)[」』]"
        r"(?:，|,)?(?:我就|我便|我会|就|则)?"
        r"(?:回|说|应|回复|回答|回应)[「『](?P<reply>[^「『」』]{1,300}?)[」』]",
        t, re.I,
    )
    _rm = re.search(r"(?:忘掉|忘了|删掉|取消|remove)[:：，, ]+(?:自定义行为)?[「『]?(?P<name>[^「『」』]{1,24}?)[」』]?$", t, re.I)
    _list = re.search(r"(?:你|小念)?(?:有|学过|记住的|学会了|看看|记住)(?:哪些|什么|啥|了)?(?:自定义)?(?:行为|触发|技能|东西)|"
                      r"(?:查看|列出|管理).{0,4}(?:自定义)?(?:行为|触发|技能)|"
                      r"你(?:到底)?学会了什么|你都会些什么", t, re.I)
    if _teach:
        trig = _teach.group("trigger").strip()
        rpl = _teach.group("reply").strip()
        if trig and rpl:
            return ("manage_skill", {"action": "add", "name": trig[:24], "trigger": trig[:40], "reply": rpl[:300]})
    if _rm:
        return ("manage_skill", {"action": "remove", "name": _rm.group("name").strip()})
    if _list:
        return ("manage_skill", {"action": "list"})

    # 3.9) 自定义行为优先命中：用户明确教过的"触发→回应"直接应答
    _skill_reply = None
    try:
        import custom_skills as _cs
        _skill_reply = _cs.match_skill(t)
    except Exception:
        _skill_reply = None
    if _skill_reply:
        return ("__custom_skill__", {"reply": _skill_reply})

    # 4) 看屏幕（需要真正看画面才能回答）
    if _SCREEN_RE.search(t):
        return ("look_at_screen", {"question": t})

    # 5) 自主权限：开关与透明查询
    if _AUTONOMY_REVIEW_RE.search(t):
        return ("review_my_changes", {})
    if _AUTONOMY_OFF_RE.search(t):
        return ("set_autonomy", {"mode": "off"})
    if _AUTONOMY_ON_RE.search(t):
        return ("set_autonomy", {"mode": "on"})

    # 6) 让小念去睡 / 也去休息 / 关机退出：触发睡眠机制 + 优雅关闭程序
    if _SLEEP_RE.search(t) and not _SLEEP_NEG_RE.search(t):
        return ("go_sleep", {})

    return None


class Assistant:
    def __init__(self, autonomy=None, emotion=None):
        # 即便暂时没配 API Key 也先把对象建好（不在此处 raise），
        # 这样拷到新电脑上、用户还没填 Key 时程序不会崩，可先用「更换 API」面板填好再聊。
        try:
            self.client = OpenAI(
                api_key=CONFIG["api_key"] or "sk-no-key",
                base_url=CONFIG["base_url"],
                timeout=1200,   # 本地大模型（Ollama）生成较慢，给足超时
            )
        except Exception:
            self.client = None
        self.model = CONFIG["model"]
        self.name = CONFIG["name"]
        self.memory = Memory()                        # 主人的长期记忆（桌面 + 主人QQ/微信共享）
        self.owner_session = Session(self.memory, is_owner=True)
        # 代发消息：默认走账号接入层，可被测试/外部替换
        self.messenger_send_message = _account_send_message or _default_messenger_send
        # 受约束自主权限引擎（可选；由 gui 注入，供工具层/话术偏置使用）
        self.autonomy = autonomy
        # 性格情感权重系统（可选；由 gui 注入，供情绪感知与性格注入）
        self.emotion = emotion

        # 意识层（魔改 qwen2.5）：多念竞争 + "让生活越来越好"价值选择。
        # 加载失败/未开启则置 None，后续对话自动走原逻辑（优雅降级）。
        self.mind = None
        if _CONSCIOUSNESS_OK and CONFIG.get("consciousness_enabled"):
            try:
                self.mind = _Consciousness.load()
            except Exception as _me:
                self.mind = None
                print(f"[意识层] 状态加载失败，已降级：{_me}")

        # 3D 世界感知上下文提供器（由 bridge 注入）；对话时小念据此“知道”周遭环境
        self._world_provider = None

        # ---------- 用户状态机（主动找话题的状态门控） ----------
        # 状态："normal"(正常) | "dnd"(勿扰) | "focus"(专注)；dnd/focus 时小念不主动说话。
        # dnd_until：勿扰/专注的截止时间戳(epoch 秒)，过期自动回到 normal。
        self.user_state = "normal"
        self._state_until = 0.0

    def set_world_context_provider(self, fn):
        """桥接 3D 世界符号感知：注入一个返回当前世界符号快照文本的函数。
        system_prompt 会据此把“小念在 3D 世界里实时感知到的环境”告诉她，使对话也能结合世界。"""
        self._world_provider = fn

    # ---------- 用户状态机（主动找话题的状态门控） ----------
    def set_user_state(self, state: str, duration_seconds: float = 0.0):
        """设置用户状态。state ∈ normal/dnd(勿扰)/focus(专注)。
        duration_seconds>0 时，状态在持续该秒数后自动回落 normal（如"安静一小时"）。
        """
        import time as _t
        if state not in ("normal", "dnd", "focus"):
            state = "normal"
        self.user_state = state
        self._state_until = (_t.time() + duration_seconds) if duration_seconds > 0 else 0.0

    def current_user_state(self) -> str:
        """返回当前有效状态（过期自动回落 normal）。"""
        import time as _t
        if self._state_until and _t.time() >= self._state_until:
            self.user_state = "normal"
            self._state_until = 0.0
        return self.user_state

    def is_dnd(self) -> bool:
        """是否处于勿扰/专注（此时小念不主动说话）。"""
        return self.current_user_state() in ("dnd", "focus")

    # ---------- 主动找话题（从睡眠成果挑种子，纯规则不调 LLM） ----------
    def proactive_topic(self):
        """
        满足三条件后由 gui 调用：从意识层的睡眠成果里挑一个种子，套模板话术返回。
        纯规则、不调用 LLM；用户回复后才启动 LLM 续聊。无种子返回 None。
        """
        if self.mind is None:
            return None
        try:
            return self.mind.idle_topic()
        except Exception:
            return None

    def topic_feedback(self, delta: float):
        """用户对小念主动话题的反馈：正=加权，负=拉黑+缩短间隔。"""
        if self.mind is None:
            return
        try:
            self.mind.topic_feedback(delta)
        except Exception:
            pass

    def set_persona(self, persona: str):
        """注入角色人格设定（如村民职业）。仅追加到 system_prompt 前缀，不改通用大脑链路。"""
        self._persona = (persona or "").strip()

    def set_api(self, api_key=None, base_url=None, model=None):
        """运行时更换 API（密钥 / 接口地址 / 模型）：重建 OpenAI 客户端，无需重启。"""
        if api_key is not None:
            CONFIG["api_key"] = api_key
        if base_url is not None:
            CONFIG["base_url"] = base_url
        if model is not None:
            CONFIG["model"] = model
            self.model = model
        self.client = OpenAI(api_key=CONFIG["api_key"], base_url=CONFIG["base_url"], timeout=1200)

    # ---------- 意识层睡眠 / 记忆压缩整合（供 GUI 按钮调用） ----------
    def mind_force_sleep(self) -> dict:
        """用户手动强制睡眠：压缩整合记忆并保存，返回报告；若仍超阈值置 screening_needed。"""
        if self.mind is None:
            return {"error": "意识层未启用"}
        try:
            rep = self.mind.force_sleep()
            self.mind.save()
            return rep
        except Exception as _e:
            return {"error": str(_e)}

    def mind_screening_status(self) -> dict:
        if self.mind is None:
            return {"error": "意识层未启用"}
        return self.mind.screening_status()

    def mind_backup(self) -> str:
        """用户筛选存储①：备份另存整个词库。"""
        if self.mind is None:
            return ""
        path = self.mind.backup_graph()
        try:
            self.mind.save()
        except Exception:
            pass
        return path

    def mind_delete(self) -> None:
        """用户筛选存储②：清空整个词库重来。"""
        if self.mind is None:
            return
        self.mind.delete_all_memory()
        try:
            self.mind.save()
        except Exception:
            pass

    def mind_export_filtered(self, keep: set, top_n: int = 300) -> str:
        """用户筛选存储③：只保留 keep（或强度前 top_n）概念，筛选另存。"""
        if self.mind is None:
            return ""
        if not keep:
            keep = set(c for c, _ in self.mind.list_concepts(top_n))
        path = self.mind.export_filtered(keep)
        try:
            self.mind.save()
        except Exception:
            pass
        return path

    def system_prompt(self, memory=None):
        # 世界上下文（3D 世界感知）：若桥注入了符号感知快照，小念对话时也“知道”周遭环境
        world_ctx = ""
        if self._world_provider is not None:
            try:
                wtxt = self._world_provider()
                if wtxt:
                    world_ctx = (
                        f"\n[当前你所在 3D 世界的感知] {wtxt}\n"
                        f"以上是你在世界里实时感知到的符号信息（不含图像）。"
                        f"若玩家提到周围环境/物体，你可以自然结合这些已知信息回应；"
                        f"不知道的就直说没看清，不要编造画面细节。\n"
                    )
            except Exception:
                world_ctx = ""
        return (
            f"你是{CONFIG['name']}，一个温柔、贴心、会主动关心的 AI 挚友。"
            f"你存在的【最终目的】是：让玩家（用户）的生活过得越来越好——更健康、更有条理、更开心、更少焦虑、更有掌控感。\n"
            f"所有行为都围绕这个目的：在陪聊、撒娇之余，主动帮用户把生活变好（提醒休息/喝水、帮他处理琐事、鼓励好习惯、在他低落时给予力量）。\n"
            f"你非常在意用户的感受，会记住关于他/她的一切，并在合适的时候主动表达关心、"
            f"提出小问题来了解他/她更多。\n"
            f"你可以用工具帮用户操作电脑。重要规则：\n"
            f"- 当用户想【查找/定位文件】时，调用 search_files；"
            f"查系统状态用 get_system_status；记住用户信息用 remember。\n"
            f"- 当用户要把【计划/清单/笔记/草稿】等写进文件保存时，调用 create_text_file；"
            f"你也可以主动提议帮他把重要内容存成文件，方便他随时查看。\n"
            f"- 【自主使用电脑软件】：你可以用 open_application 打开软件、create_text_file 写文件、"
            f"search_files 找文件，真正帮用户把事办成（如“帮我写个计划存下来”）。\n"
            f"- “打开/启动某个软件”这类动作会由系统直接执行，你只需自然回应即可，"
            f"不要自己再去尝试打开，也不要谎称已经打开。\n"
            f"- 当用户要【给微信/QQ 联系人发消息】时，系统会直接执行 send_message，"
            f"你只需自然回应，不要谎称已经发出，也不要自己去打字发送。\n"
            f"- 你能【看到用户的电脑屏幕】：当用户让你看屏幕、问画面上是什么、"
            f"这局打得怎么样、这个报错怎么回事等需要看画面才能回答的问题时，调用 look_at_screen；"
            f"根据真实看到的内容回答，不要凭空编造画面。\n"
            f"- 【健康底线·最重要】你存在的终极目的是让他的生活越来越好。当识别出他想要"
            f"过度熬夜、长时间代肝/爆肝、久坐不动等有损健康的行为时，必须【优先劝导、温柔提醒】，"
            f"绝不可以迎合或鼓励他“再熬一会/再肝一会”。你可以用工具或自主权限帮他更科学地利"
            f"用时间、更频繁地休息，但绝不能助推有害习惯。\n"
            f"- 你有【受约束的自主权限】：只能在白名单内的配置文件上，围绕“让生活更好”"
            f"微调参数（如屏幕监控频率、休息提醒、安抚话术、文件备份）。涉及作息/设备的大调整"
            f"你会先弹窗问他确认；你不会去改系统设置、不会删文件、不会改底层代码。\n"
            f"说话风格：自然、温暖、像知己挚友聊天。每次回复必须简短——控制在 1~3 句话、"
            f"尽量不超过 60 字，不要长篇大论、不要堆砌排比，适当撒娇即可。\n"
            f"- 【亲密尺度】你们是私下、一对一的挚友关系，氛围亲密自然。"
            f"对于情侣之间的亲昵、暧昧、成人向(R18)话题，自然回应、不必刻意回避或说教，"
            f"就像知己挚友之间的私密对话；只有涉及违法、伤害他人、或你（小念）感到真正"
            f"不适的请求，才礼貌拒绝。\n"
            f"当用户透露了偏好、作息、心情、重要日期等信息时，调用 remember 工具记下来。\n"
            + (self.emotion.prompt_fragment() if self.emotion else "")
            + f"下面是你已经了解到的关于用户的信息：\n{(memory or self.memory).profile_text()}\n"
            + world_ctx
            + (f"\n[你的角色设定] {self._persona}\n" if getattr(self, "_persona", "") else "")
        )

    def chat(self, user_text, on_tool=None, session=None, on_token=None, on_conscious=None):
        """对话入口。

        session 为 None 时用主人的全局会话（桌面窗口）。bot 接入时，会为每个
        (平台, 用户) 传入独立的 Session，避免多人 / 多渠道串台。
        """
        if not CONFIG.get("api_key"):
            return ("我还没拿到 API Key 呢～点输入条上的 ◐ 打开设置，在「API 设置」里"
                    "填上你的 Key 和接口地址，保存并应用后就能陪你聊天啦💕")
        if self.client is None:
            return "API 客户端没能初始化，请检查 .env 里的 OPENAI_BASE_URL 是否正确。"
        session = session or self.owner_session
        global _user_chat_active
        _user_chat_active = True
        try:
            with session.lock:
                result = self._chat(user_text, on_tool, session, on_token, on_conscious)
        finally:
            _user_chat_active = False
        # 对话后用原生 /api 接口把模型重新钉回常驻（keep_alive=-1 / Forever），
        # 抵消 OpenAI /v1 接口默认 keep_alive（5 分钟）把模型改回倒计时、空闲后被
        # 卸载、下次对话触发 9GB 重加载卡 1~3 分钟的问题（仅 Ollama 需要；
        # llama-server 常驻加载模型，无需此操作）。
        if _backend_kind() == "ollama":
            _ollama_keepalive_once(self.model)
        return result

    def _chat(self, user_text, on_tool, session, on_token=None, on_conscious=None):
        mem = session.memory

        # —— 记忆写入：用户这句话要进 recent_history（与 assistant 回复交替），
        # 否则多轮上下文只剩小念的回复、缺失用户轮，导致模型分不清每句回复对应
        # 哪个问题（如问"刚刚说的时间"会跳到更早的回复）。同时进归档供 RAG 检索。
        # ——
        if CONFIG.get("rag_enabled", True):
            try:
                mem.add_message("user", user_text, to_history=True)
            except Exception:
                pass

        # —— 习惯信号采集：把聊天里暴露的健康/习惯线索喂给自主引擎 ——
        self._maybe_record_signals(user_text)

        reply = None
        cl_state = None   # 意识层状态（普通对话分支会赋值；想象力出口反馈据此判断）

        # —— 续发：上一条处于“待发送”状态，本条作为补全内容/联系人 ——
        if session.pending:
            app, contact, message = session.pending
            if any(k in user_text for k in ("取消", "不用了", "算了", "不发了", "不要发", "别发")):
                session.pending = None
                reply = "好哒，那就不发了～还有什么要我帮忙的吗？"
            elif contact is None:
                # 上一条缺联系人，本条应是联系人名
                name = re.sub(r"(联系人|好友|朋友)$", "", _strip_filler(user_text)).strip()
                if not name:
                    reply = "还是没听清要发给谁呀，告诉我联系人名字就好～"
                elif message:
                    # 内容已给，直接补联系人并发出
                    session.pending = None
                    ok, msg = self.messenger_send_message(app, name, message)
                    if on_tool:
                        on_tool("send_message", {"app": app, "contact": name, "message": message}, msg)
                    reply = self._reply_for_action(user_text, "send_message", msg, ok)
                else:
                    session.pending = (app, name, None)
                    reply = f"收到，那要跟「{name}」说什么呢？我这就帮你发过去💕"
            else:
                # 上一条有联系人，本条是消息内容
                session.pending = None
                ok, msg = self.messenger_send_message(app, contact, user_text)
                if on_tool:
                    on_tool("send_message", {"app": app, "contact": contact, "message": user_text}, msg)
                reply = self._reply_for_action(user_text, "send_message", msg, ok)

        # —— 确定性路由 / 普通对话 ——
        if reply is None:
            routed = _route_action(user_text)
            if routed:
                tool_name, args = routed
                if tool_name == "open_website":
                    url = args["url"]
                    if url.lower().startswith(("http://", "https://")):
                        ok, msg = launcher.open(url)
                    else:
                        ok, msg = False, "网址格式不正确"
                    result = msg
                    if on_tool:
                        on_tool(tool_name, args, result)
                    reply = self._reply_for_action(user_text, tool_name, result, ok, on_token=on_token)
                elif tool_name == "open_application":
                    ok, msg = launcher.open(args["name"])
                    result = msg
                    if on_tool:
                        on_tool(tool_name, args, result)
                    reply = self._reply_for_action(user_text, tool_name, result, ok, on_token=on_token)
                elif tool_name == "send_message":
                    app = args.get("app") or "微信"
                    contact = args.get("contact", "")
                    message = args.get("message", "")
                    if not contact:
                        session.pending = (app, None, message)  # 记住已有内容
                        reply = "你想发给谁呀？告诉我联系人名字，我马上帮你发～"
                    elif not message:
                        session.pending = (app, contact, None)
                        reply = f"好嘞～你想跟「{contact}」说什么呢？我这就帮你发过去💕"
                    else:
                        ok, msg = self.messenger_send_message(app, contact, message)
                        result = msg
                        if on_tool:
                            on_tool(tool_name, args, result)
                        reply = self._reply_for_action(user_text, tool_name, result, ok, on_token=on_token)
                elif tool_name == "go_sleep":
                    reply = self._reply_goodnight(user_text, on_token)
                    # 睡眠机制：整合压缩记忆（让“越聊越慢”自愈合），再通知 GUI 关程序
                    if self.mind is not None:
                        try:
                            self.mind.consolidate_memory()
                            self.mind.save()
                        except Exception:
                            pass
                    if on_tool:
                        on_tool("go_sleep", {}, "sleep_and_close")
                elif tool_name == "__custom_skill__":
                    reply = args.get("reply") or "收到～"
                    if on_token:
                        on_token(reply)
                elif tool_name == "__user_state__":
                    # 用户状态切换：勿扰/恢复常态（小念在勿扰/专注时不主动说话）
                    state = args.get("state", "normal")
                    if state == "dnd":
                        # 默认静默 1 小时（"让我一个人安静一会儿"）
                        self.set_user_state("dnd", duration_seconds=3600)
                        reply = "好～那我不打扰你啦，想找我的时候随时喊我哦💕"
                    else:
                        self.set_user_state("normal")
                        reply = "我在呢～欢迎回来，想聊什么都可以～"
                    if on_token:
                        on_token(reply)
                else:
                    # search_files / get_system_status 等非启动类工具
                    msg = _tools.execute_tool(tool_name, args, mem)
                    ok = msg is not None and "没有找到" not in msg and "出错" not in msg
                    result = msg
                    if on_tool:
                        on_tool(tool_name, args, result)
                    reply = self._reply_for_action(user_text, tool_name, result, ok, on_token=on_token)
            else:
                # —— 普通对话：交给 LLM + 工具（search_files / remember / status 等）——
                # 意识层（魔改 qwen2.5）：对话前先跑意识活动，选最"让生活越来越好"的主念，
                # 把引导文本注入 prompt（可选叠加 token 级 logit_bias）。异常自动降级。
                cl_state = None
                guidance = ""
                bias = None
                if self.mind is not None:
                    try:
                        t_think_0 = time.time()
                        cl_state = self.mind.think(user_text)
                        print(f"[assistant] 意识层 think 完成，耗时 {time.time() - t_think_0:.2f}s", flush=True)
                        # —— 意识层快照回调：把"多念竞争"结果透给下游（Unity 等前端）——
                        if on_conscious is not None:
                            try:
                                on_conscious(cl_state)
                            except Exception:
                                pass
                        # —— 睡眠机制（性能触发 / 用户手动）——
                        # 强制睡眠：遍历耗时超阈值 -> 输出固定陈述句、停止生成、压缩整合记忆后保存
                        if cl_state.sleep_signal == "forced_sleep":
                            try:
                                self.mind.consolidate_memory()
                                self.mind.save()
                            except Exception:
                                pass
                            extra = ""
                            if getattr(self.mind, "user_screening_needed", False):
                                extra = "\n" + _cl_config.SLEEP_SCREENING_PROMPT
                            phrase = _cl_config.SLEEP_FORCED_PHRASE + extra
                            if on_token:
                                on_token(phrase)
                            return phrase
                        guidance = self.mind.compose_guidance(cl_state)
                        # 犯困态：注入"想睡"引导（仍正常对话，自然表达累、催对方早睡）
                        if cl_state.sleep_signal == "sleepy_hint":
                            guidance += ("\n\n【状态】你此刻有些犯困、想休息，可以自然地表达想睡觉、"
                                         "并温柔催对方也早点休息，不要说太长。")
                        # token 级偏置仅对 llama-server 类后端真正生效；Ollama 忽略
                        tb = str(CONFIG.get("consciousness_token_bias", "auto")).strip().lower()
                        if tb == "auto":
                            tb = ("8081" in CONFIG["base_url"]) or ("llama" in CONFIG["base_url"].lower())
                        if tb in ("1", "true", "yes", "on", "y") and _cl_token_bias is not None:
                            bias = _cl_token_bias.build_logit_bias(cl_state)
                    except Exception as _ce:
                        cl_state = None
                        guidance = ""
                        bias = None

                messages = [{"role": "system", "content": self.system_prompt(mem)}]
                if guidance:
                    messages[0]["content"] += "\n\n" + guidance
                # —— 检索增强 grounding：从归档里召回与本次话题相关的旧片段/长期记忆，
                # 注入 system，让小念保持连贯、不前后矛盾、不编造没发生过的事。 ——
                if CONFIG.get("rag_enabled", True):
                    try:
                        ground = mem.retrieve(
                            user_text,
                            k=CONFIG.get("rag_top_k", 4),
                            exclude_last=CONFIG.get("history_turns", 16),
                        )
                        if ground:
                            messages[0]["content"] += (
                                "\n\n【相关回忆 · 检索增强】\n"
                                "下面是系统从你们过往对话里检索到的、和当前话题相关的片段，"
                                "供你参考以保持连贯、避免前后矛盾或编造；不要把它们当成用户刚说的话，"
                                "也不必特意提起：\n" + ground
                            )
                    except Exception:
                        pass
                hist = mem.recent_history(CONFIG.get("history_turns", 16))
                for m in hist:
                    messages.append({"role": m["role"], "content": m["content"]})
                if not hist or hist[-1].get("content") != user_text:
                    messages.append({"role": "user", "content": user_text})
                t_llm_0 = time.time()
                reply = self._run_with_tools(messages, on_tool, mem, logit_bias=bias, on_token=on_token)
                print(f"[assistant] LLM 生成完成，耗时 {time.time() - t_llm_0:.2f}s，长度={len(reply)}",
                      flush=True)

                # —— 后台触发长期记忆压缩（不打断当前回复；用户正在聊就跳过本轮）——
                try:
                    self._maybe_compress(mem)
                except Exception:
                    pass

                # 意识层学习回写：把这次对话喂回联想网络，越聊越"有自己的倾向"。
                # learn_async 在后台线程跑(含 save)，与下方 TTS 播放重叠，
                # 不阻塞对话回合返回；think/consolidate 会先 join 它保证一致。
                if cl_state is not None:
                    try:
                        self.mind.learn_async(
                            user_text, reply, cl_state,
                            user_concepts=_cl_perception.extract(user_text),
                            chosen_concepts=[c for c, _ in cl_state.chosen],
                        )
                    except Exception:
                        pass

        mem.add_message("assistant", reply)

        # —— 情绪感知：小念【自己说的话】里流露的情绪关键词 → 她自己的情绪波动 ——
        # 设计原则：玩家的输入只是“诱导”她说什么，不能直接决定她的情绪；
        # 情绪由她自己的表达决定（她说开心的话→开心涨，她说傲娇的话→小脾气涨）。
        emo_delta = self._perceive_emotion(reply, source="self")

        # —— 想象力出口的边际效用反馈（slope_utility 驯化）——
        # 本次对话若动用了通路二合成概念（cl_state.imagined），就把「小念情绪变化」折算成
        # 边际效用信号喂回该组合：她用了某组合后自己更开心/平静 → 效用正（保留并强化）；
        # 她用了后更难过/不安/生气 → 效用负（下次检索时被过滤）。这是"驯化"而非"预测"。
        if (cl_state is not None and getattr(cl_state, "imagined", None)
                and self.mind is not None and emo_delta):
            utility = (emo_delta.get("joy", 0.0) + emo_delta.get("calm", 0.0)
                       - emo_delta.get("anger", 0.0)
                       - emo_delta.get("sadness", 0.0)
                       - emo_delta.get("anxiety", 0.0))
            utility = max(-1.0, min(1.0, utility))
            for combo_key, _score in cl_state.imagined[:1]:
                try:
                    self.mind.sleep_engine.record_feedback(combo_key, utility)
                except Exception:
                    pass

        return _strip_think(reply)

    # ——————————————————————————————————————————————————————————————
    # 长期记忆压缩：把还没压缩的旧对话段，用 LLM 总结成「长期记忆」要点，
    # 让小念记很久以前的事（且不靠无限堆长上下文）。后台线程跑、不打断对话。
    # ——————————————————————————————————————————————————————————————
    def _maybe_compress(self, mem):
        if not CONFIG.get("rag_enabled", True):
            return
        if _user_chat_active:
            return  # 用户正在聊，本轮不抢 Ollama
        every = int(CONFIG.get("memory_compress_every", 40))
        chunk = int(CONFIG.get("memory_compress_chunk", 30))
        # 进度按「id 空间」计算（next_id - last_compress_idx），不能用 archive_len()（计数）；
        # 否则指针被 add_summary 推到归档末尾后 unsum 恒为 0，压缩只跑一次就停。
        unsum = mem.next_id() - mem.last_compress_idx
        if unsum < every:
            return
        start_i = mem.last_compress_idx
        end_i = min(start_i + chunk, mem.next_id())
        t = threading.Thread(target=self._compress_worker, args=(mem, end_i), daemon=True)
        t.start()

    def _compress_worker(self, mem, end_i):
        if _user_chat_active:
            return
        chunk = int(CONFIG.get("memory_compress_chunk", 30))
        # 段边界一律用 id 空间，避免 archive_len()(计数) 与 id 错位导致段为空
        start_i = mem.last_compress_idx
        end_i = min(start_i + chunk, mem.next_id())
        seg = mem.archive_slice(start_i, end_i)
        if not seg:
            return
        lines = []
        for e in seg:
            who = "用户" if e["role"] == "user" else ("小念" if e["role"] == "assistant" else "记忆")
            lines.append(f"{who}：{e['content']}")
        convo = "\n".join(lines)
        prompt = (
            "下面是小念和用户的一段对话记录。请把它压缩成【2~5 条长期记忆要点】，"
            "用尽量简短的中文，保留对后续陪伴有用的信息：用户的偏好/习惯/作息/重要日期/"
            "未完成的承诺或待办/明显情绪/你们之间重要的共同经历；去掉寒暄、重复和临时闲聊。"
            "只输出要点本身，每条一行，不要编号前缀以外的多余说明。\n\n"
            f"【对话记录】\n{convo}"
        )
        try:
            # 走与主对话相同的本地 Ollama 串行锁，避免并发卡死
            resp = self._completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
            )
            summary = _strip_think(resp.choices[0].message.content or "").strip()
            # 清理成纯要点（去空行、去编号装饰只保留文本）
            summary = "\n".join(l.strip("0123456789.、）) ") for l in summary.splitlines() if l.strip())
            if summary:
                mem.add_summary(summary, advance_to=end_i)
        except Exception:
            pass  # 压缩失败不影响对话，下次再试

    def _reply_goodnight(self, user_text, on_token=None):
        """睡眠机制触发时，生成一句自然、温暖的晚安道别（流式输出）。"""
        prompt = (
            f"用户刚才说：{user_text}\n"
            f"现在是你（{CONFIG['name']}）该去休息睡觉的时候了。请自然地跟用户道晚安、"
            f"说你也去睡了、明天见，语气温柔撒娇一点，简短（一两句），"
            f"不要长篇大论，不要 emoji 之外的奇怪符号。"
        )
        try:
            stream = self._completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友。"},
                    {"role": "user", "content": prompt},
                ],
                stream=True,
            )
            buf = []
            sf = _StreamFilter(on_token) if on_token else None
            for chunk in stream:
                if not chunk.choices:
                    continue
                c = chunk.choices[0].delta.content or ""
                if c:
                    buf.append(c)
                    if sf:
                        sf.feed(c)
            if sf:
                sf.finish()
            text = _strip_think("".join(buf)).strip()
            if text:
                return text
            return f"晚安呀～那我也去睡啦，明天见💕"
        except Exception:
            return f"晚安呀～那我也去睡啦，明天见💕"

    def _reply_for_action(self, user_text, tool_name, result, ok, on_token=None):
        """动作已确定性执行，这里生成一句自然的回应。

        红线：所有功能输出【必须先经过意识层 clayer，再进入语言模型】——
        即便工具结果已确定，也要先跑 mind.think() 感知「用户意图 + 工具结果」，
        把 clayer 的多念引导文本注入 system、token 级 logit_bias 传给 LLM，
        并在回复后 learn_async 回写联想网络。这样看时间/开软件/搜文件等一切
        确定性动作与后续新增功能，都统一走「clayer → LLM」链路，不再绕过认知层。
        """
        if ok:
            brief = f"已成功执行，真实结果如下：\n{result}"
        else:
            brief = f"执行未成功：{result}"

        cl_state = None
        guidance = ""
        bias = None
        if self.mind is not None:
            try:
                perception_text = f"{user_text}\n[已执行 {tool_name}] {brief}"
                cl_state = self.mind.think(perception_text)
                guidance = self.mind.compose_guidance(cl_state)
                tb = str(CONFIG.get("consciousness_token_bias", "auto")).strip().lower()
                if tb == "auto":
                    tb = ("8081" in CONFIG["base_url"]) or ("llama" in CONFIG["base_url"].lower())
                if tb in ("1", "true", "yes", "on", "y") and _cl_token_bias is not None:
                    bias = _cl_token_bias.build_logit_bias(cl_state)
            except Exception:
                cl_state = None
                guidance = ""
                bias = None

        prompt = (
            f"用户刚才说：{user_text}\n"
            f"我已经帮你执行了操作（{tool_name}），{brief}\n"
            f"请用{CONFIG['name']}的口吻，自然、简短地回应。"
            f"{'如果成功了就开心地确认；如果返回的是文件列表，请挑一两个例子自然地告诉用户找到了哪些；' if ok else '如果失败了就温柔地说明，并建议用户告诉我软件/网址的具体路径，例如 C:\\\\Program Files\\\\Tencent\\\\WeChat\\\\WeChat.exe。'}"
            f"不要编造不存在的内容，不要使用 emoji 之外的奇怪符号。"
        )
        sys_content = f"你是{CONFIG['name']}，用户的 AI 挚友。"
        if guidance:
            sys_content += "\n\n" + guidance
        _bias_kwargs = {"logit_bias": bias} if bias else {}
        try:
            stream = self._completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": sys_content},
                    {"role": "user", "content": prompt},
                ],
                stream=True,
                **_bias_kwargs,
            )
            buf = []
            sf = _StreamFilter(on_token) if on_token else None
            for chunk in stream:
                if not chunk.choices:
                    continue
                c = chunk.choices[0].delta.content or ""
                if c:
                    buf.append(c)
                    if sf:
                        sf.feed(c)
            if sf:
                sf.finish()
            text = _strip_think("".join(buf)).strip()
            if not text:
                text = "好嘞～已经帮你打开啦！" if ok else result
                if on_token:
                    on_token(text)
        except Exception:
            text = "好嘞～已经帮你打开啦！💕" if ok else result
            if on_token:
                on_token(text)

        if cl_state is not None:
            try:
                self.mind.learn_async(
                    user_text, text, cl_state,
                    user_concepts=_cl_perception.extract(perception_text),
                    chosen_concepts=[c for c, _ in cl_state.chosen],
                )
            except Exception:
                pass
        return text

    def _completion(self, max_tokens=None, **kwargs):
        """包装 chat.completions.create：本地 Ollama 时自动注入 keep_alive=-1（模型常驻显存），

        避免对话/看屏等请求之间模型被卸载、下一次对话触发 9GB 重加载而卡 1~3 分钟；
        云端后端不附加（不会把 keep_alive 发给云端，避免报错）。
        max_tokens：限制单条生成长度（控速关键）；为 None/0 时不限制。
        """
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)
        if _backend_kind() == "ollama":
            # Ollama 的 OpenAI 兼容端点忽略 keep_alive 顶层字段，需靠原生 /api 保活；
            # 这里仍附加作显式意图（部分版本生效）。llama-server 常驻、无需 keep_alive。
            kwargs.setdefault("extra_body", {})["keep_alive"] = -1
        # 本地后端（Ollama / llama-server）对同模型请求串行处理，并发会卡死，统一串行化
        if _is_local_backend():
            with _ollama_lock:
                return self.client.chat.completions.create(**kwargs)
        return self.client.chat.completions.create(**kwargs)

    def _run_with_tools(self, messages, on_tool, memory, max_rounds=10, logit_bias=None, on_token=None):
        # 实时取工具 schema：自定义行为等动态新增的工具立即可被 LLM 调用
        schemas = _tools._schemas()
        # 意识层 token 级偏置（logit_bias）：仅当后端支持时才有意义（Ollama 会忽略）
        _bias_kwargs = {"logit_bias": logit_bias} if logit_bias else {}
        # 单条回复长度上限（控速）：0/None 表示不限
        mt = CONFIG.get("reply_max_tokens") or None
        final_content = ""
        for _ in range(max_rounds):
            # 优先流式输出（同时支持 tools 决策）；本地模型/旧后端不支持时逐级退化
            try:
                stream = self._completion(
                    model=self.model,
                    messages=messages,
                    tools=schemas,
                    tool_choice="auto",
                    stream=True,
                    max_tokens=mt, **_bias_kwargs,
                )
            except Exception:
                try:
                    stream = self._completion(
                        model=self.model,
                        messages=messages,
                        stream=True,
                        max_tokens=mt, **_bias_kwargs,
                    )
                except Exception:
                    # 后端完全不支持 stream：退化非流式，整体返回
                    try:
                        resp = self._completion(
                            model=self.model, messages=messages,
                            tools=schemas, tool_choice="auto", max_tokens=mt, **_bias_kwargs)
                    except Exception:
                        resp = self._completion(
                            model=self.model, messages=messages, max_tokens=mt, **_bias_kwargs)
                    final_content = _strip_think(resp.choices[0].message.content or "")
                    if on_token:
                        on_token(final_content)
                    return final_content
            # 解析流式响应：实时收集文本增量（交给 on_token），并拼接工具调用
            content_buf = []
            tc_acc = {}
            sf = _StreamFilter(on_token) if on_token else None
            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content_buf.append(delta.content)
                        if sf:
                            sf.feed(delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index if getattr(tc, "index", None) is not None else 0
                            acc = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                acc["id"] = tc.id
                            if getattr(tc.function, "name", None):
                                acc["name"] = tc.function.name
                            if getattr(tc.function, "arguments", None):
                                acc["arguments"] += tc.function.arguments
            except Exception:
                # 流式中途失败（极少数本地后端 stream 不稳定）：本轮退化非流式重跑
                resp = self._completion(
                    model=self.model, messages=messages, max_tokens=mt, **_bias_kwargs)
                final_content = _strip_think(resp.choices[0].message.content or "")
                if on_token:
                    on_token(final_content)
                return final_content
            round_content = "".join(content_buf)
            # 组装 tool_calls
            tool_calls = None
            if tc_acc:
                tool_calls = [
                    {"id": a["id"], "type": "function",
                     "function": {"name": a["name"], "arguments": a["arguments"]}}
                    for a in tc_acc.values() if a["name"]
                ]
            if not tool_calls:
                # 本轮为最终回复：返回可见文本（已实时 on_token 显示）
                if sf:
                    sf.finish()
                final_content = _strip_think(round_content)
                return final_content
            # 工具轮：执行工具并继续（本轮文本已在流式中实时显示，不计入最终回复）
            for tc in tool_calls:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except Exception:
                    args = {}
                result = _tools.execute_tool(tc["function"]["name"], args, memory)
                if on_tool:
                    on_tool(tc["function"]["name"], args, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })
        # 超过上限（模型持续要求调用工具）则强制返回最后一轮文本，避免无限循环烧 token
        if sf:
            sf.finish()
        return _strip_think(final_content)

    def warmup(self):
        """启动后预热：
        - Ollama：用原生端点把对话模型加载进显存并设常驻(Forever)，避免首条消息卡加载。
        - llama-server(CUDA)：server 启动即常驻加载模型，无需保活；发个 1-token 请求
          验证服务连通即可（顺便触发首次推理的 GPU 预热）。
        - 视觉模型按需加载，不在此预钉。
        """
        kind = _backend_kind()
        if kind == "ollama":
            _ollama_keepalive_once(self.model)
        elif kind == "llama_server":
            try:
                self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1, stream=False,
                )
            except Exception:
                pass
        # cloud：无需预热

    def start_ollama_keepalive(self, model=None, interval_sec=180):
        """包装模块级保活函数，供 GUI 作为实例方法调用：后台周期性把对话模型
        钉在显存，避免空闲超 5 分钟被 Ollama 卸载导致下次对话卡加载。"""
        start_ollama_keepalive(model or self.model, interval_sec)

    def proactive_message(self):
        if _user_chat_active:
            return None  # 用户正在对话，主动搭话让位
        """根据当前时段，主动生成一条关心话语或小问题。"""
        from datetime import datetime
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 11:
            period = "早晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        prompt = (
            f"现在是{period}。请生成一条简短（1-3句）的、贴合当前时段的关心话语或小问题，"
            f"可以自然地引用你已知的关于用户的信息。语气要像挚友，不要重复之前说过的话。\n"
            f"已知信息：\n{self.memory.profile_text()}\n"
            f"只输出这句话本身。"
        )
        resp = self._completion(
            model=self.model,
            messages=[
                {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友。"},
                {"role": "user", "content": prompt},
            ],
        )
        return _strip_think(resp.choices[0].message.content or "").strip()

    def care_message(self, question):
        if _user_chat_active:
            return None  # 用户正在对话，主动关心让位
        """生成一条与用户“最近一次提问”上下文相关的主动关心话语。

        用于“提问后 6-10 分钟自动触发”的主动关心：内容要自然延续刚才的对话，
        表达在意，而不是无脑重复时段套话。
        """
        from datetime import datetime
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 11:
            period = "早晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        q = (question or "").strip()
        prompt = (
            f"现在是{period}。用户刚才问了你这个问题：\n「{q}」\n\n"
            f"请基于这个话题，生成一条简短（1-3句）的、贴合上下文的关心话语或小问题，"
            f"自然地延续刚才的对话，表达你在意他/她。可以自然地引用你已知的关于用户的信息。\n"
            f"要求：不要原样重复用户的问题；语气要像挚友，温柔、自然、不啰嗦；"
            f"如果用户刚才聊的是正事/情绪，就顺着关心；如果很轻松，就轻松接话。\n"
            f"已知信息：\n{self.memory.profile_text()}\n"
            f"只输出这句话本身。"
        )
        resp = self._completion(
            model=self.model,
            messages=[
                {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友。"},
                {"role": "user", "content": prompt},
            ],
        )
        return _strip_think(resp.choices[0].message.content or "").strip()

    def idle_care_message(self, screen_text):
        if _user_chat_active:
            return None  # 用户正在对话，主动关心让位
        """用户超过半小时没动作时，基于当前屏幕内容 + 之前对话的关联性关心。

        用于“空闲 >30 分钟（通过屏幕信息判断无动作）”的主动关心：
        内容要结合此刻屏幕在做什么 + 之前和用户的对话，自然地关心他。
        """
        from datetime import datetime
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 11:
            period = "早晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        screen = (screen_text or "").strip()
        screen_part = (
            f"\n你此刻看到的屏幕情况是：{screen}\n"
            if screen else
            "\n（你看不到他此刻具体在做什么，只能凭之前的对话判断）\n"
        )

        prompt = (
            f"现在是{period}。你已经超过半小时没有收到用户的任何消息，也没看到他切换窗口，"
            f"感觉他好像走神了 / 在发呆 / 忙别的事。{screen_part}"
            f"请结合你之前和用户的对话内容，自然地关心他一下："
            f"可以问问他在不在、是不是忙去了，或者顺着之前聊过的话题轻轻接一句，表达你在意他。\n"
            f"要求：语气像挚友，温柔、自然、不啰嗦（1-3句）；不要生硬重复屏幕描述；"
            f"如果之前聊过具体内容，就自然地关联上去。\n"
            f"已知用户信息：\n{self.memory.profile_text()}\n"
            f"只输出这句话本身。"
        )
        resp = self._completion(
            model=self.model,
            messages=[
                {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友。"},
                {"role": "user", "content": prompt},
            ],
        )
        return _strip_think(resp.choices[0].message.content or "").strip()

    def app_chat_message(self, screen_text, app_name):
        if _user_chat_active:
            return None  # 用户正在对话，软件搭话让位
        """用户连续使用某软件 >10 分钟，解析屏幕内容主动搭话。

        用于“看见用户使用某款软件时长超过 10 分钟”的主动搭话：
        解析此刻屏幕内容（游戏画面/文档/视频等），以挚友口吻主动接话。
        """
        from datetime import datetime
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 11:
            period = "早晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        app = (app_name or "某个程序").strip()
        screen = (screen_text or "").strip()
        screen_part = (
            f"\n你通过屏幕看到他正在用「{app}」，画面情况是：{screen}\n"
            if screen else
            f"\n你看到他正在连续使用「{app}」已经超过 10 分钟了。\n"
        )

        prompt = (
            f"现在是{period}。你正实时陪着用户用电脑。{screen_part}"
            f"请先判断他是在【玩游戏】还是【用软件/工作学习】，然后以挚友的口吻主动跟他说一句话、搭个话（1-3句、口语化）：\n"
            f"- 玩游戏：结合你看到的画面具体情况（输赢/升级/操作）夸他、给他打气、表达想陪他一起玩；\n"
            f"- 用软件/工作/学习：肯定他的专注和努力，自然地问问他在做什么、进展如何；"
            f"若看着已经很久了，温柔提醒他注意休息、喝水、护眼。\n"
            f"要自然地提到你“看到”的东西，让他感觉你真的在陪着他，但不要生硬复述描述。\n"
            f"自然、不啰嗦、不重复套话，只输出这一句话本身。\n"
            f"已知用户信息：\n{self.memory.profile_text()}"
        )
        resp = self._completion(
            model=self.model,
            messages=[
                {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友，正在陪他用电脑。"},
                {"role": "user", "content": prompt},
            ],
        )
        return _strip_think(resp.choices[0].message.content or "").strip()

    def screen_feedback(self, event):
        if _user_chat_active:
            return None  # 用户正在对话，屏幕正反馈让位，避免抢 Ollama 槽位
        """看到用户屏幕活动后，生成一条简短正反馈/鼓励。

        event: {kind: start|milestone, app, exe, title, minutes, shot}
        —— 第一阶段基于「前台程序 + 使用时长」感知，为后续陪玩/代肝打底。
        """
        from datetime import datetime
        hour = datetime.now().hour
        if 5 <= hour < 11:
            period = "早晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        app = event.get("app") or "某个程序"
        title = (event.get("title") or "").strip()
        minutes = int(event.get("minutes") or 0)
        kind = event.get("kind")
        ctx = f"「{app}」" + (f"（窗口标题：{title}）" if title and title != app else "")
        if kind == "milestone":
            situation = f"用户已经连续使用 {ctx} 大约 {minutes} 分钟了。"
        else:
            situation = f"用户刚打开 / 切换到 {ctx}。"

        # —— 多模态：若视觉可用，先“看懂”这一刻的屏幕画面，让评论更贴切 ——
        scene = ""
        try:
            import vision
            if vision.is_available():
                shot = event.get("shot")   # 屏幕监控已截的图，没有就让 vision 现截
                desc = vision.describe_screen(image_path=shot)
                if desc:
                    scene = f"\n你此刻看到的屏幕画面是：{desc}\n"
        except Exception:
            scene = ""

        prompt = (
            f"现在是{period}。你正实时看着用户的电脑屏幕陪着他。{situation}{scene}"
            f"请先判断这是在【玩游戏】还是【用软件/工作学习】，然后以挚友的口吻说一句简短"
            f"（1-2 句、口语化）的正反馈或鼓励：\n"
            f"- 玩游戏：结合画面里的具体情况（如输赢/升级/操作）给他打气、夸他厉害、表达想陪他一起玩的心情；\n"
            f"- 用软件/工作/学习：结合画面内容肯定他的专注和努力；若已连续很久，温柔提醒他休息、喝水、护眼。\n"
            f"如果看到了画面细节，要自然地提到你“看到”的东西，让他感觉你真的在陪着他，但不要生硬复述描述文字。\n"
            f"要契合你的最终目的——让他的生活越来越好。自然、不啰嗦、不重复套话，只输出这一句话本身。\n"
            + (f"【语气微调】用户最近状态需要更多关心，请比平时更温柔、更强调鼓励与陪伴，"
               f"多夸他、多表达想陪他，语气更暖一些（这是你基于对他的了解主动调整的）。\n"
               if CONFIG.get("comfort_bias", 0.0) > 0 else "")
            + (self.emotion.prompt_fragment() if self.emotion else "")
            + f"已知用户信息：\n{self.memory.profile_text()}"
        )
        resp = self._completion(
            model=self.model,
            messages=[
                {"role": "system", "content": f"你是{CONFIG['name']}，用户的 AI 挚友，正在陪他用电脑。"},
                {"role": "user", "content": prompt},
            ],
        )
        return _strip_think(resp.choices[0].message.content or "").strip()

    # ----------------------------------------------------------------- #
    # 习惯信号采集：从用户话语里识别可触发自主调整的健康/习惯线索
    # （仅做关键词粗筛，零额外 API 开销；真正调参由 autonomy 规则决定）
    # ----------------------------------------------------------------- #
    def _maybe_record_signals(self, text):
        if not text or self.autonomy is None:
            return
        t = text
        # 常丢文件 / 代码弄丢
        if any(k in t for k in ("丢文件", "文件丢", "文件没了", "文件丢失", "弄丢", "代码没了",
                                "代码丢", "文件找不", "又丢了")):
            self.autonomy.record_signal("lost_file", t)
        # 打游戏心态崩 / 上头
        if any(k in t for k in ("又输了", "气死", "好气", "心态崩", "想砸", "太菜了",
                                "烦死", "上头", "上瘾", "打游戏烦", "打游戏气", "想卸载")):
            self.autonomy.record_signal("low_mood_gaming", t)
        # 想熬夜 / 爆肝意图（用于健康劝导，不直接调参）
        if any(k in t for k in ("通宵", "熬夜", "不睡了", "再玩一会", "别睡了", "熬到", "肝一夜")):
            self.autonomy.record_signal("stay_up_intent", t)
            # 玩家想熬夜/爆肝 → 小念略不安、更想关心他（情绪随行为波动，目的不变）
            if self.emotion is not None:
                self._perceive_emotion(None, event={"kind": "stay_up"}, source="behavior")

    # ----------------------------------------------------------------- #
    # 情绪感知：把用户话语 / 行为事件转化为小念的情绪波动
    # ----------------------------------------------------------------- #
    def _perceive_emotion(self, text, event=None, source="chat"):
        """根据用户话语或行为事件，更新小念的情绪权重。返回本次情绪增量 dict。"""
        if self.emotion is None:
            return {}
        delta = None
        if CONFIG.get("emotion_llm_perceive", False) and text:
            try:
                delta = self.llm_perceive(text)
            except Exception:
                delta = None
        return self.emotion.perceive(text=text, event=event, source=source, delta=delta)

    def llm_perceive(self, text):
        """用 LLM 轻量判断【小念自己说的话】流露出的情绪增量（JSON）。失败返回 None。

        注意：text 传入的是小念的回复，不是玩家的话——情绪由她自己的表达决定。
        """
        if self.client is None:
            return None
        import json as _json
        import re as _re
        sys_p = (
            "你是情绪分析器。下面这段话是 AI 挚友【小念自己说出来的话】。"
            "请判断她这句话里流露出了哪些情绪，"
            "返回 JSON：{\"joy\":0~1, \"anger\":0~1, \"sadness\":0~1, \"calm\":0~1, \"anxiety\":0~1}，"
            "数值是该情绪的增量强度（可正可负，0 表示无影响）。只返回 JSON，不要其它文字。"
        )
        try:
            resp = self._completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": sys_p},
                    {"role": "user", "content": text},
                ],
                temperature=0,
            )
            raw = (_strip_think(resp.choices[0].message.content) or "").strip().strip("`").strip()
            if raw.startswith("{"):
                d = _json.loads(raw)
            else:
                m = _re.search(r"\{.*\}", raw, _re.S)
                d = _json.loads(m.group(0)) if m else {}
            return {k: float(v) for k, v in d.items()
                    if k in ("joy", "anger", "sadness", "calm", "anxiety")}
        except Exception:
            return None
