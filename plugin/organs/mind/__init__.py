"""心念：她第一次说话要用的那一层——给 messages，回一段字。

mind 只管一件事：模型客户端（llm.py）。模型名与地址在 data/llm.json，key 在 .env，
失败按 kind 分人话。不读魂/脑/心，不认框架，也不做装配——装配在别人手里。
"""
from __future__ import annotations

from .llm import (
    LLMConfigError,
    LLMError,
    complete,
    default_transport,
    env_candidates,
    find_env_file,
    load_config,
    parse_env,
    read_key,
)

__all__ = [
    "LLMError",
    "LLMConfigError",
    "complete",
    "parse_env",
    "find_env_file",
    "env_candidates",
    "read_key",
    "load_config",
    "default_transport",
]
