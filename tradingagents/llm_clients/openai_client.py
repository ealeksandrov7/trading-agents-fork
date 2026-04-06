import os
from copy import deepcopy
from typing import Any, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from pydantic import PrivateAttr

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). This normalizes to string for consistent
    downstream handling.
    """

    _gemma_thinking: bool = PrivateAttr(default=False)
    _provider_name: str = PrivateAttr(default="openai")

    def __init__(self, *args, gemma_thinking: bool = False, provider_name: str = "openai", **kwargs):
        super().__init__(*args, **kwargs)
        self._gemma_thinking = gemma_thinking
        self._provider_name = provider_name

    def invoke(self, input, config=None, **kwargs):
        if self._gemma_thinking:
            input = maybe_enable_gemma_thinking(input)
        return normalize_content(super().invoke(input, config, **kwargs))

# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs and API key env vars
_PROVIDER_CONFIG = {
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        # Provider-specific base URL and auth
        if self.provider in _PROVIDER_CONFIG:
            base_url, api_key_env = _PROVIDER_CONFIG[self.provider]
            llm_kwargs["base_url"] = base_url
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        return NormalizedChatOpenAI(
            gemma_thinking=self.kwargs.get("gemma_thinking", False),
            provider_name=self.provider,
            **llm_kwargs,
        )

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)


def is_gemma_ollama_model(provider: str, model: str) -> bool:
    return provider.lower() == "ollama" and model.lower().startswith("gemma4:")


def maybe_enable_gemma_thinking(input_data):
    """Prefix the first system prompt with the Gemma think token."""
    if not isinstance(input_data, list):
        return input_data

    updated = list(input_data)
    for idx, message in enumerate(updated):
        content = None
        if isinstance(message, tuple) and len(message) >= 2 and message[0] == "system":
            content = str(message[1])
            if not content.lstrip().startswith("<|think|>"):
                updated[idx] = ("system", f"<|think|>\n{content}")
            return updated

        if isinstance(message, dict) and message.get("role") == "system":
            content = str(message.get("content", ""))
            if not content.lstrip().startswith("<|think|>"):
                new_message = deepcopy(message)
                new_message["content"] = f"<|think|>\n{content}"
                updated[idx] = new_message
            return updated

        role = getattr(message, "type", None)
        if role == "system":
            content = str(getattr(message, "content", ""))
            if not content.lstrip().startswith("<|think|>"):
                if hasattr(message, "model_copy"):
                    updated[idx] = message.model_copy(
                        update={"content": f"<|think|>\n{content}"}
                    )
                elif hasattr(message, "copy"):
                    updated[idx] = message.copy(update={"content": f"<|think|>\n{content}"})
                else:
                    updated[idx] = SystemMessage(content=f"<|think|>\n{content}")
            return updated
    return updated
