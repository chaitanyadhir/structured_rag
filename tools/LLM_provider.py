"""
One place for LLM access. Use anywhere:

    from tools.llm_provider import llm_call
    answer = llm_call("Summarise this ...")      # -> str

Config (environment variables):
    ANTHROPIC_API_KEY   required
    LLM_MODEL           default "claude-sonnet-5-5"
    LLM_MAX_TOKENS      default 4096
    LLM_TIMEOUT         seconds, default 60

To switch provider later (OpenAI, Bedrock, local...), change only this file.
"""
import os


class LLMError(RuntimeError):
    """The model call failed (network, rate limit, bad response...)."""


class LLMConfigError(LLMError):
    """Missing key / package: retrying will not help."""


_client = None


def _get_client():
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LLMConfigError("ANTHROPIC_API_KEY is not set")
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise LLMConfigError("The 'anthropic' package is not installed (pip install anthropic)") from e
        _client = Anthropic(api_key=key, timeout=float(os.getenv("LLM_TIMEOUT", "60")), max_retries=2)
    return _client


def llm_call(prompt: str, *, system: str | None = None, max_tokens: int | None = None) -> str:
    """Send a text prompt, get the model's text answer back."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    client = _get_client()
    kwargs = {"model": os.getenv("LLM_MODEL", "claude-sonnet-5-5"),
              "max_tokens": max_tokens or int(os.getenv("LLM_MAX_TOKENS", "4096")),
              "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:                    # SDK raises many types; callers only need one
        raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    if not text.strip():
        raise LLMError("LLM returned no text")
    return text