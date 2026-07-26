"""多模态视觉能力：让小念真正“看懂”屏幕画面。

背景：主对话模型（DeepSeek deepseek-chat）不支持图像输入，所以这里走一个
【独立的、OpenAI 兼容的多模态视觉 API】。默认指向智谱 GLM-4V-Flash（有免费额度，
只需在 .env 填 VISION_API_KEY 即可用）；也可换成 Qwen-VL / gpt-4o 等任何兼容
OpenAI /chat/completions + image_url 的服务。

对外提供：
- is_available()            视觉是否已正确配置（开关 + key）
- capture(save_path=None)   截当前屏幕，压缩成小尺寸 JPEG，返回文件路径
- look(question, image_path=None)  截图/读图 → 调视觉模型 → 返回一段文字理解

设计要点：
- 截图后按 VISION_MAX_WIDTH 等比缩小 + JPEG 压缩，显著降低延迟与 token 消耗。
- 任何环节失败都优雅降级返回 None / 空串，绝不让上层崩溃（视觉是增强项）。
- 视觉客户端与主对话客户端相互独立（不同 base_url / key / model）。
"""

import os
import base64

from config import CONFIG

_client = None


def is_available():
    """视觉功能是否可用：需 VISION_ENABLED=true 且填了 VISION_API_KEY。"""
    return bool(CONFIG.get("vision_enabled") and CONFIG.get("vision_api_key"))


def _get_client():
    """懒加载独立的视觉 API 客户端（OpenAI 兼容）。"""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(
            api_key=CONFIG.get("vision_api_key", ""),
            base_url=CONFIG.get("vision_base_url", "https://open.bigmodel.cn/api/paas/v4"),
        )
    return _client


def set_vision_api(api_key=None, base_url=None, model=None):
    """运行时更换视觉 API（密钥 / 接口地址 / 模型），重置客户端缓存，无需重启。

    由 GUI 的「视觉 API 设置」面板调用；改完后会立刻用新配置（下次 look 生效）。
    """
    global _client
    if api_key is not None:
        CONFIG["vision_api_key"] = api_key
    if base_url is not None:
        CONFIG["vision_base_url"] = base_url
    if model is not None:
        CONFIG["vision_model"] = model
    _client = None  # 强制下次懒加载时用新配置


def capture(save_path=None, max_width=None):
    """截当前整个屏幕 → 等比缩小 + JPEG 压缩，返回图片路径；失败返回 None。"""
    try:
        from PIL import ImageGrab
    except Exception:
        return None
    if not save_path:
        d = os.path.join(CONFIG.get("data_dir", "."), "screen_watch")
        os.makedirs(d, exist_ok=True)
        save_path = os.path.join(d, "vision.jpg")
    max_width = int(max_width or CONFIG.get("vision_max_width", 1280))
    try:
        img = ImageGrab.grab()  # 主屏
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if w > max_width:
            img = img.resize((max_width, int(h * max_width / w)))
        img.save(save_path, "JPEG", quality=80)
        return save_path
    except Exception:
        return None


def _data_uri(image_path):
    """把图片文件编码成 data URI（base64），供 image_url 使用。"""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{b64}"
    except Exception:
        return None


def look(question, image_path=None, max_tokens=400):
    """让视觉模型看一张图并回答/描述。

    question:   要模型关注/回答什么（如“描述画面重点”或“帮我看看这个报错”）。
    image_path: 指定图片；为 None 时自动截当前屏幕。
    返回：模型的文字理解；不可用或失败返回 None。
    """
    if not is_available():
        return None
    path = image_path or capture()
    if not path or not os.path.exists(path):
        return None
    uri = _data_uri(path)
    if not uri:
        return None
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=CONFIG.get("vision_model", "glm-4v-flash"),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            }],
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None


# 供屏幕陪伴用：把画面浓缩成一段“客观事实描述”，交给上层小念口吻再加工。
_SCENE_PROMPT = (
    "这是用户此刻的电脑屏幕截图。请用中文客观、简洁地描述画面里正在发生的关键信息，"
    "重点包括：正在用什么软件/玩什么游戏、当前在做的具体事情、"
    "画面上是否出现了值得注意的状态（如游戏胜利/失败/升级/结算、视频/文章标题、"
    "代码或报错内容、进度条/完成度等）。只描述你真实看到的，不要编造，不超过80字。"
)


def describe_screen(image_path=None):
    """给屏幕陪伴用：返回一段对当前画面的客观事实描述；不可用返回 None。"""
    return look(_SCENE_PROMPT, image_path=image_path, max_tokens=200)
