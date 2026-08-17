"""工具层：用装饰器注册所有可用工具，并向 assistant 暴露 TOOL_SCHEMAS / execute_tool。

open_application 直接委托给 launcher.AppLauncher（独立的启动模块），
其余工具保持轻量。新增工具只需写一个函数并用 @tool(...) 注册即可。"""

import os
import subprocess
import platform
import time as _time
import datetime

from launcher import launcher
from config import CONFIG

try:
    import psutil
except Exception:
    psutil = None


# 工具注册表：(schema, handler) 列表，handler 签名统一为 (args, memory)
TOOLS = []


def tool(schema):
    """装饰器：把一个函数注册为一个工具。"""
    def deco(fn):
        TOOLS.append((schema, fn))
        return fn
    return deco


@tool({
    "type": "function",
    "function": {
        "name": "open_application",
        "description": "打开电脑上的软件或文件，例如 微信、浏览器、记事本、某个文件路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "应用名或文件路径，例如 '微信'、'notepad'、'C:\\a.txt'"}
            },
            "required": ["name"],
        },
    },
})
def open_application(args, memory):
    ok, msg = launcher.open(args.get("name", ""))
    return msg


@tool({
    "type": "function",
    "function": {
        "name": "open_website",
        "description": "用默认浏览器打开一个网址。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整网址，如 https://www.baidu.com"}
            },
            "required": ["url"],
        },
    },
})
def open_website(args, memory):
    url = args.get("url", "")
    if not url:
        return "没有提供网址"
    try:
        os.startfile(url)
        return f"已在浏览器打开：{url}"
    except Exception as e:
        return f"打开网页失败：{e}"


@tool({
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "在用户电脑里按文件名关键字搜索文件，返回匹配的路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件名关键字或通配符，如 'report*.docx'"},
                "path": {"type": "string", "description": "搜索起始目录，默认用户目录"}
            },
            "required": ["pattern"],
        },
    },
})
def search_files(args, memory):
    pattern = args.get("pattern", "")
    base = args.get("path") or os.path.expanduser("~")
    matches = []
    for root, _, files in os.walk(base):
        for f in files:
            if pattern.lower() in f.lower():
                matches.append(os.path.join(root, f))
                if len(matches) >= 10:
                    break
        if len(matches) >= 10:
            break
    return "\n".join(matches) if matches else "没有找到匹配的文件。"


@tool({
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "在用户电脑上执行一条命令行命令（如查看进程、开关服务、运行脚本）。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"}
            },
            "required": ["command"],
        },
    },
})
def run_command(args, memory):
    command = args.get("command", "")
    if not command:
        return "没有提供命令"
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        out = (result.stdout or "") + (result.stderr or "")
        return out[:2000] if out else "命令已执行，无输出。"
    except Exception as e:
        return f"命令执行出错：{e}"


@tool({
    "type": "function",
    "function": {
        "name": "get_system_status",
        "description": "获取电脑当前状态：操作系统、CPU 核数、内存占用。",
        "parameters": {"type": "object", "properties": {}},
    },
})
def get_system_status(args, memory):
    if psutil is None:
        return (f"系统: {platform.system()} {platform.release()}\n"
                f"（未安装 psutil，无法读取 CPU/内存）")
    return (f"系统: {platform.system()} {platform.release()}\n"
            f"CPU 核数: {psutil.cpu_count()}\n"
            f"内存占用: {psutil.virtual_memory().percent}%")


@tool({
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "设置一个定时提醒：在指定的分钟数之后，主动提醒用户一件事"
            "（喝水/休息/吃药/开会/关机等）。当用户说“X分钟后提醒我Y”"
            "“过半小时叫我”“5分钟后叫我喝水”等需要定时提醒时使用。"
            "真正的计时由系统执行，到点会主动弹出并语音播报提醒。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delay_min": {
                    "type": "number",
                    "description": "多少分钟之后提醒（>0；如 5 表示 5 分钟后）。",
                },
                "message": {
                    "type": "string",
                    "description": "到点时要提醒用户的话，如 '该喝水啦' '该起来活动一下了'。",
                },
            },
            "required": ["delay_min", "message"],
        },
    },
})
def set_reminder(args, memory):
    try:
        delay_min = float(args.get("delay_min", 0))
        message = (args.get("message") or "").strip()
    except Exception:
        return "提醒时间格式不对，请告诉我几分钟后提醒。"
    if delay_min <= 0:
        return "提醒时间要大于 0 分钟哦，再告诉我一次吧。"
    if not message:
        return "没听清要提醒你什么，告诉我提醒的内容吧。"
    # 参数里已携带 delay_min/message，GUI 的 on_tool 会据此真正启动定时器。
    # 这里只做登记确认，返回结构化信息供 assistant 自然回应。
    return f"已登记定时提醒：{delay_min} 分钟后提醒「{message}」。"


@tool({
    "type": "function",
    "function": {
        "name": "reminder_status",
        "description": (
            "查看当前所有待触发的定时提醒（还有多久触发）。"
            "当用户问“你设了哪些提醒/还有多久提醒/提醒状态”时使用。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
})
def reminder_status(args, memory):
    # 真正的状态由 GUI 的 on_tool 读取显示；这里仅作占位确认
    return "提醒状态已查询（界面会显示当前待触发的提醒）。"


@tool({
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "取消一个已设定的定时提醒（传要取消的提醒编号 id）。"
            "当用户说“取消提醒/别提醒我了/把那个提醒关掉”时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "要取消的提醒编号，如 1。"},
            },
            "required": ["id"],
        },
    },
})
def cancel_reminder(args, memory):
    return f"已请求取消提醒 #{args.get('id', '')}。"


@tool({
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "在互联网上搜索信息。当你遇到不会做的事、需要实时数据、"
            "不确定某件事该怎么做时，先搜索再行动。比如“怎么设置定时器”"
            "“Python 如何用 root.after 定时”等。返回搜索结果的标题+摘要+URL。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，建议用中文描述你想知道什么。",
                },
                "max_results": {
                    "type": "number",
                    "description": "最多返回几条结果（默认 5）。",
                },
            },
            "required": ["query"],
        },
    },
})
def web_search(args, memory):
    try:
        import search
    except Exception:
        return "搜索模块未加载。"
    q = (args.get("query") or "").strip()
    n = int(args.get("max_results", 5) or 5)
    if not q:
        return "想搜什么呀？告诉我关键词～"
    results = search.search(q, max_results=n)
    return search.format_results(results, q)


@tool({
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "获取一个网页的正文内容（纯文本）。当你从搜索结果中看到一个"
            "可能有详细答案的页面时，用它打开看具体内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要打开的网页 URL。",
                },
            },
            "required": ["url"],
        },
    },
})
def web_fetch(args, memory):
    try:
        import search
    except Exception:
        return "搜索模块未加载。"
    url = (args.get("url") or "").strip()
    if not url:
        return "要打开哪个网页呀？给我 URL～"
    return search.fetch(url)


@tool({
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "获取当前系统时间：日期（年/月/日）、时刻（时/分）、星期几、以及"
            "所处时段（清晨/上午/中午/下午/傍晚/晚上/深夜）。当用户问"
            "“现在几点/今天几号/今天是星期几/几月几日/现在是什么时候/现在几点了”等"
            "需要知道当前时间才能回答的问题时使用；你也可以自主调用它来掌握时间，"
            "以便提醒作息、判断时段、更好地关心用户。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
})
def get_current_time(args, memory):
    now = datetime.datetime.now()
    # 星期中文
    weekday = "一二三四五六日"[now.weekday()]
    # 时段划分
    h = now.hour
    if h < 5:
        period = "深夜"
    elif h < 8:
        period = "清晨"
    elif h < 11:
        period = "上午"
    elif h < 13:
        period = "中午"
    elif h < 17:
        period = "下午"
    elif h < 19:
        period = "傍晚"
    elif h < 23:
        period = "晚上"
    else:
        period = "深夜"
    return (f"现在是 {now.year}年{now.month}月{now.day}日 星期{weekday}，"
            f"{now.hour:02d}:{now.minute:02d}（{period}）。")


@tool({
    "type": "function",
    "function": {
        "name": "look_at_screen",
        "description": (
            "看用户当前的电脑屏幕并理解画面内容。当用户说“看看我的屏幕/看下我在干嘛/"
            "帮我看看这个/屏幕上是什么/这个报错怎么回事/我这局打得怎么样”等需要看画面才能"
            "回答的问题时使用。会自动截图并用视觉模型理解。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "针对屏幕要看/要回答的具体问题，如 '帮我看看这个报错' '我这局赢了吗'；没有具体问题就描述画面重点。",
                }
            },
        },
    },
})
def look_at_screen(args, memory):
    try:
        import vision
    except Exception:
        return "小念暂时看不到屏幕（视觉模块加载失败）。"
    if not vision.is_available():
        return ("小念还没有开启看屏能力。请在 .env 里设置 VISION_ENABLED=true 并填写 "
                "VISION_API_KEY（可用智谱 GLM-4V 等多模态模型的 key）。")
    question = (args.get("question") or "").strip()
    prompt = (
        (question + "\n" if question else "")
        + "请仔细看这张电脑屏幕截图，用中文说明画面里正在发生什么、"
          "有哪些值得注意的信息（软件/游戏、正在做的事、输赢/进度/报错等），只描述真实看到的。"
    )
    result = vision.look(prompt, max_tokens=500)
    return result or "小念没看清屏幕，可能是截图失败或视觉服务无响应，稍后再试试～"


@tool({
    "type": "function",
    "function": {
        "name": "remember",
        "description": "记住关于用户的一个事实或偏好，例如爱好、作息、心情、重要日期。",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "要记住的内容，一句话描述"}
            },
            "required": ["fact"],
        },
    },
})
def remember(args, memory):
    fact = args.get("fact", "")
    memory.remember_fact(fact)
    return f"好的，我已经记住了：{fact}"


@tool({
    "type": "function",
    "function": {
        "name": "set_autonomy",
        "description": (
            "控制小念的「受约束自主权限」开关。当用户说“你自己看着调/你帮我盯着/交给你了/"
            "你自己决定/关掉自主/听我的/别自己改”时使用。开启后小念可在白名单内围绕“让生活更好”"
            "微调配置（大调整仍会先问你）；关闭后所有改动由用户决定。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "on=开启小念自主调整；off=关闭，所有改动由用户决定。",
                }
            },
            "required": ["mode"],
        },
    },
})
def set_autonomy(args, memory):
    try:
        import autonomy
    except Exception:
        return "自主权限模块未加载。"
    a = autonomy.INSTANCE
    if a is None:
        return "自主权限模块未加载。"
    mode = args.get("mode", "on")
    if mode == "off":
        a.set_mode(False)
        return "好的，我已经关掉自主调整啦，之后任何设置改动都由你来定，我只提建议～"
    a.set_mode(True)
    return ("收到～我会在白名单内、只在配置文件上帮你微调，而且大改动（比如作息类）"
            "还是会先弹窗问你确认的，你随时能撤销哦💕")


@tool({
    "type": "function",
    "function": {
        "name": "tune_my_setting",
        "description": (
            "让小念基于对你的了解，自主调整某个设置（调参仍受白名单与安全护栏约束："
            "只改配置文件、大调整先问你）。当用户说“你看着把提醒调频繁点/把鼓励加强/"
            "帮我把备份打开”等要小念自己动手调参时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "要调的配置项名，如 screen_watch_min_gap_min / comfort_bias / file_backup_enabled"},
                "value": {"type": "string", "description": "目标值（数字或 true/false），由小念判断填什么"},
                "reason": {"type": "string", "description": "为什么想调，一句口语化说明"},
            },
            "required": ["key"],
        },
    },
})
def tune_my_setting(args, memory):
    try:
        import autonomy
    except Exception:
        return "自主权限模块未加载。"
    a = autonomy.INSTANCE
    if a is None:
        return "自主权限模块未加载。"
    key = args.get("key", "")
    value = args.get("value", "")
    reason = args.get("reason", "我根据你的情况想调一下这个设置。")
    return a.propose(key, value, reason, interactive=True)


@tool({
    "type": "function",
    "function": {
        "name": "review_my_changes",
        "description": "查看小念都自主改过哪些设置、最近的操作记录，保证透明可控。当用户问“你都自己改了什么/你动了我的设置吗”时使用。",
        "parameters": {"type": "object", "properties": {}},
    },
})
def review_my_changes(args, memory):
    try:
        import autonomy
    except Exception:
        return "自主权限模块未加载。"
    a = autonomy.INSTANCE
    if a is None:
        return "还没有可查看的自主改动。"
    return a.review()


@tool({
    "type": "function",
    "function": {
        "name": "create_text_file",
        "description": (
            "在你的电脑上创建一个文本/Markdown 文件并把文字内容写进去，"
            "例如帮你把计划、清单、笔记、草稿写进文件保存。当用户说"
            "“帮我写个计划（保存/存成文件）/记到文档/把这段存成文件/写到文件里”等"
            "需要落盘成文件时使用；你也可以主动提议帮他保存重要内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "文件名或相对路径，可带扩展名（如 计划.md、购物清单.txt）；不带扩展名默认 .txt。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入文件的完整文字内容（你已经生成好的内容）。",
                },
                "folder": {
                    "type": "string",
                    "description": "目标文件夹（可选）：'桌面'、'下载'、'文档'、'小念工作台'，或具体路径；不填默认存到『小念工作台』。",
                },
            },
            "required": ["filename", "content"],
        },
    },
})
def create_text_file(args, memory):
    if not CONFIG.get("file_ops_enabled", True):
        return "创建文件功能未开启（FILE_OPS_ENABLED=false），先到 .env 打开它吧～"
    filename = (args.get("filename") or "").strip()
    content = args.get("content") or ""
    folder = (args.get("folder") or "").strip()
    if not filename:
        return "没有指定文件名，告诉我你想存成什么名字～"
    if not content.strip():
        return "文件内容为空，先告诉我你想写点什么吧～"

    home = os.path.expanduser("~")
    folder_alias = {
        "桌面": "Desktop", "desktop": "Desktop",
        "下载": "Downloads", "downloads": "Downloads",
        "文档": "Documents", "documents": "Documents", "我的文档": "Documents",
        "小念工作台": "小念工作台", "工作台": "小念工作台", "workbench": "小念工作台",
    }
    if folder:
        key = folder_alias.get(folder.lower(), folder)
        if os.path.isabs(key):
            target_dir = key
        elif key in ("Desktop", "Downloads", "Documents"):
            target_dir = os.path.join(home, key)
        else:
            target_dir = os.path.join(home, "Documents", key)
    else:
        target_dir = os.path.join(home, "Documents", "小念工作台")

    # 解析最终路径
    if os.path.isabs(filename):
        full = os.path.normpath(filename)
    else:
        full = os.path.normpath(os.path.join(target_dir, filename))
    norm_full = os.path.normcase(full)

    # 安全护栏：禁止写入系统/程序目录，避免误伤系统
    blocked = (r"\windows\system32", r"\windows\syswow64", r"\windows\winsxs",
               r"\program files", r"\program files (x86)")
    if any(b in norm_full for b in blocked):
        return "为了安全，小念不能往系统/程序目录里写文件哦～换个桌面或文档里的位置吧。"

    if not os.path.splitext(full)[1]:
        full += ".txt"

    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
    except Exception as e:
        return f"创建文件夹失败：{e}"

    # 不覆盖已有文件：自动加序号
    if os.path.exists(full):
        base, ext = os.path.splitext(full)
        i = 1
        while os.path.exists(f"{base}({i}){ext}"):
            i += 1
        full = f"{base}({i}){ext}"

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"写文件失败：{e}"

    return f"已经帮你把内容存到文件啦：{full}"


@tool({
    "type": "function",
    "function": {
        "name": "feelings_status",
        "description": (
            "查看小念此刻的情绪与性格底色（开心/生气/伤心/平静/不安，以及当前性格，"
            "如温柔平静/活泼开心/傲娇小脾气/敏感爱哭/黏人紧张）。当用户问"
            "“你现在心情怎么样/你是什么性格/你生我气了吗/你是不是不开心”等关心小念状态时使用。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
})
def feelings_status(args, memory):
    try:
        import emotion
    except Exception:
        return "小念的情感模块未加载。"
    e = emotion.INSTANCE
    if e is None:
        return "小念的情感模块未加载。"
    return e.describe()


@tool({
    "type": "function",
    "function": {
        "name": "manage_skill",
        "description": (
            "管理小念的『自定义行为』——让她自己学会处理一类新的重复性请求，"
            "不用开发改代码。动作有：add（新增一个行为：定义触发词和固定回应）、"
            "remove（删除一个行为）、list（列出已有行为）。当用户说"
            "“以后我说X你就回Y/记住这个/你学会一下/帮我记住怎么做”等，"
            "或想查看/取消某个自定义行为时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "list"],
                    "description": "add=新增；remove=删除；list=查看全部。",
                },
                "name": {
                    "type": "string",
                    "description": "行为名字（仅 add/remove 需要），如 '开工加油'。",
                },
                "trigger": {
                    "type": "string",
                    "description": "触发词（仅 add 需要）：用户说这句话（包含它）就触发，如 '开工'。",
                },
                "reply": {
                    "type": "string",
                    "description": "触发后的回应文本（仅 add 需要），如 '好嘞～开工大吉！加油哦💪'。",
                },
            },
            "required": ["action"],
        },
    },
})
def manage_skill(args, memory):
    try:
        import custom_skills as cs
    except Exception:
        return "自定义行为模块未加载。"
    action = (args.get("action") or "").strip().lower()
    if action == "list":
        return cs.format_list()
    if action == "add":
        ok, msg = cs.add_skill(args.get("name", ""), args.get("trigger", ""), args.get("reply", ""))
        return msg
    if action == "remove":
        ok, msg = cs.remove_skill(args.get("name", ""))
        return msg
    return "不支持的动作，试试 add / remove / list 吧～"


# 供 assistant 使用的 schema 列表（运行时实时构建，动态新增的工具立即可用）
def _schemas():
    return [schema for schema, _ in TOOLS]


TOOL_SCHEMAS = _schemas()


def execute_tool(name, args, memory):
    for schema, handler in TOOLS:
        if schema["function"]["name"] == name:
            try:
                return handler(args, memory)
            except Exception as e:
                return f"执行工具 {name} 出错：{e}"
    return f"未知工具：{name}"
