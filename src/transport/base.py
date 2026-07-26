import abc
import logging


class BotTransport(abc.ABC):
    """一个平台（QQ / 微信）的接入抽象。

    子类负责连接具体协议（OneBot / gewechat / …），并实现下面四个方法。
    收到消息时调用 self.on_message(platform, user_id, text, display_name, **ctx)。
    """

    platform = ""  # 子类填 "QQ" / "微信"

    def __init__(self, on_message):
        self.on_message = on_message  # callable(platform, user_id, text, name, **ctx)
        self._log = logging.getLogger(f"transport.{self.platform or 'base'}")
        self.running = False

    @abc.abstractmethod
    def start(self):
        """启动连接（通常在后台线程里运行）。"""
        ...

    @abc.abstractmethod
    def stop(self):
        """停止连接。"""
        ...

    @abc.abstractmethod
    def send_to_user(self, user_id, text, **ctx):
        """给某个用户 / 群回消息，返回 (ok: bool, msg: str)。"""
        ...

    @abc.abstractmethod
    def send_to_contact(self, contact, text):
        """按名字或账号给联系人 / 群发消息（最佳努力解析），返回 (ok: bool, msg: str)。"""
        ...
