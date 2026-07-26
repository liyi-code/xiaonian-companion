"""小念的 IM 接入层。

每个平台（QQ / 微信）对应一个 BotTransport 实例，负责：
  - 接收该平台的私聊 / 群消息，回调 on_message(platform, user_id, text, name, **ctx)
  - 把小念的回复发回（send_to_user / send_to_contact）

接入层与“大脑” assistant.py 完全解耦：大脑只管生成回复，不知道消息来自哪个软件。
"""

# 支持的平台标识
PLATFORMS = ("QQ", "微信")
