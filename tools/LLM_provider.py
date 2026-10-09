"""
One place for LLM access. Use anywhere:

    from tools.LLM_provider import llm_call
    answer = llm_call("Summarise this ...")      # -> str

Config (environment variables):
    GROQ_API_KEY        required
    LLM_MODEL           default "qwen/qwen3.8-27b"
    LLM_MAX_TOKENS      default 4096
    LLM_TEMPERATURE     default 0 (deterministic SQL / JSON)
    LLM_TIMEOUT         seconds, default 60

To switch provider later (OpenAI, Bedrock, local...), change only this file.
"""
from dotenv import load_dotenv
import os

load_dotenv()

class LLMError(RuntimeError):
    """The model call failed (network, rate limit, bad response...)."""


class LLMConfigError(LLMError):
    """Missing key / package: retrying will not help."""


_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise LLMConfigError("GROQ_API_KEY is not set")
        try:
            from groq import Groq
        except ImportError as e:
            raise LLMConfigError("The 'groq' package is not installed (pip install groq)") from e
        _client = Groq(api_key=key, timeout=float(os.getenv("LLM_TIMEOUT", "60")), max_retries=2)
    return _client


def warm_up() -> None:
    """Build the client at startup; silently skip if not configured."""
    try:
        _get_client()
    except LLMConfigError:
        pass


def llm_call(prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
    """Send a text prompt, get the model's text answer back."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    client = _get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {
        "model": os.getenv("LLM_MODEL", "qwen/qwen3.8-27b"),
        "max_tokens": max_tokens or int(os.getenv("LLM_MAX_TOKENS", "4096")),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0")),
        "messages": messages,
    }
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as e:                    # SDK raises many types; callers only need one
        raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e
    
    text = response.choices[0].message.content or ""
    if not text.strip():
        raise LLMError("LLM returned no text")
    return text