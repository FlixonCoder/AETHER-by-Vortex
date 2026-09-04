"""
LLM Client Bridge.
Maintains backward compatibility while delegating to the 3-Tier LLMProvider and Rule Engine.
"""
from .llm_provider import LLMProvider

_provider_instance = None

def get_llm_provider() -> LLMProvider:
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = LLMProvider()
    return _provider_instance

def get_client():
    return get_llm_provider()
