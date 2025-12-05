from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "schema": {
        "text": [
            "text",
            "comment",
            "comment_text",
            "content",
            "message",
            "body",
            "评论",
            "评论内容",
            "留言",
            "内容",
        ],
        "platform": ["platform", "source", "channel", "平台", "来源"],
        "author": [
            "author",
            "username",
            "user",
            "display_name",
            "用户名",
            "用户",
            "作者",
        ],
        "record_id": ["record_id", "comment_id", "sample_id", "id", "评论id", "编号"],
    },
    "quality": {
        "display_issue_exact_values": ["0"],
        "replacement_markers": ["�", "□"],
        "notice_prefixes": ["已由"],
        "notice_contains": ["置頂", "置顶", "設為置頂"],
        "boilerplate_contains": [
            "for a more in-depth analysis",
            "new video out every friday",
            "check out the original report here",
        ],
        "minimum_signal_characters": 2,
    },
    "classification": {
        "positive": {
            "good": 0.8,
            "great": 1.0,
            "excellent": 1.0,
            "helpful": 0.7,
            "clear": 0.5,
            "love": 1.0,
            "like": 0.6,
            "thanks": 0.45,
            "感谢": 0.45,
            "很好": 0.9,
            "优秀": 1.0,
            "支持": 0.65,
            "赞": 0.75,
        },
        "negative": {
            "bad": -0.8,
            "poor": -0.7,
            "wrong": -0.7,
            "scam": -1.0,
            "fraud": -1.0,
            "fake": -0.9,
            "risk": -0.65,
            "danger": -0.85,
            "unsafe": -0.85,
            "broken": -0.7,
            "hate": -1.0,
            "stupid": -0.85,
            "problem": -0.55,
            "crash": -0.8,
            "not helpful": -1.0,
            "not clear": -0.9,
            "high": -0.45,
            "expensive": -0.7,
            "失望": -0.8,
            "骗局": -1.0,
            "危险": -0.9,
            "糟糕": -0.9,
            "问题": -0.55,
            "不好": -0.9,
        },
        "topics": {
            "pricing": ["price", "cost", "fee", "expensive", "价格", "收费", "贵"],
            "reliability": ["stable", "bug", "error", "broken", "crash", "稳定", "错误", "崩溃"],
            "support": ["help", "support", "reply", "服务", "客服", "帮助"],
            "safety": ["risk", "danger", "safe", "unsafe", "安全", "风险", "危险"],
        },
        "negative_subtypes": {
            "risk_concern": ["risk", "danger", "unsafe", "安全", "风险", "危险"],
            "distrust_suspicion": ["scam", "fraud", "fake", "lie", "骗局", "欺骗", "怀疑"],
            "sarcasm_attack": ["stupid", "idiot", "hate", "蠢", "笨", "恨"],
        },
        "meta_feedback": [
            "great explanation",
            "clear walkthrough",
            "nice video",
            "thanks for sharing",
            "感谢分享",
            "讲得好",
        ],
        "positive_threshold": 0.15,
        "negative_threshold": -0.15,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load optional JSON configuration on top of safe built-in defaults."""

    if path is None:
        return deepcopy(DEFAULT_CONFIG)
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise TypeError("Configuration root must be a JSON object.")
    return _deep_merge(DEFAULT_CONFIG, loaded)
