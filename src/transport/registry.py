"""已启用的 transport 注册表 + 统一发送入口。

bot.py 在启动时为每个开启的平台调用 register(transport)；
assistant 里的“代发消息”最终也走这里的 send_message。
"""

_transports = {}  # platform -> BotTransport


def register(t):
    _transports[t.platform] = t


def get(platform):
    return _transports.get(platform)


def all_transports():
    return list(_transports.values())


def send_message(app, contact, message):
    """按 app('微信'/'QQ') 找到对应 transport 并发送；找不到则给出友好提示。"""
    t = _transports.get(app) or _transports.get("QQ") or next(iter(_transports.values()), None)
    if not t:
        return (False, "小念还没有连接任何账号（QQ/微信未启用），无法代为发送。")
    return t.send_to_contact(contact, message)
