# 模型积分消耗映射表（固定消耗/次）
MODEL_CREDITS: dict[str, int] = {
    # 图像生成
    "nano-banana-pro": 1800,
    "nano-banana-2": 1300,
    "nano-banana-pro-vt": 1800,
    "nano-banana-fast": 440,
    "nano-banana": 1400,
    # 其他模型保留
    "sora-image": 400,
    "gpt-image-1.5": 400,
    "nano-banana-pro-cl": 3400,
    "nano-banana-pro-vip": 7000,
    "nano-banana-pro-4k-vip": 8600,
    "sora-2": 1600,
    "veo3.1-fast-1080p": 3200,
    "veo3.1-pro-4k": 14000,
    "veo3.1-pro-1080p": 10000,
    "veo3.1-pro": 8000,
    "veo3.1-fast": 2400,
    "veo3.1-fast-4k": 4800,
    "sora-create-character": 200,
    "sora-upload-character": 200,
}

# 按 token 计算的模型
TOKEN_BASED_MODELS = {
    "gemini-3.1-pro",
    "gemini-3-pro",
    "gemini-2.5-pro",
}


def get_model_cost(model: str) -> int:
    """Return fixed credits cost for a model, or 0 if token-based / unknown."""
    return MODEL_CREDITS.get(model, 0)
