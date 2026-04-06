import unittest

from types import SimpleNamespace

from tradingagents.llm_clients.base_client import normalize_content, strip_gemma_thinking
from tradingagents.llm_clients.openai_client import (
    is_gemma_ollama_model,
    maybe_enable_gemma_thinking,
)


class GemmaOllamaSupportTests(unittest.TestCase):
    def test_catalog_detection_for_gemma_ollama(self):
        self.assertTrue(is_gemma_ollama_model("ollama", "gemma4:e4b"))
        self.assertFalse(is_gemma_ollama_model("openai", "gemma4:e4b"))
        self.assertFalse(is_gemma_ollama_model("ollama", "qwen3:latest"))

    def test_maybe_enable_gemma_thinking_on_system_tuple(self):
        messages = [("system", "You are helpful."), ("human", "Hi")]
        updated = maybe_enable_gemma_thinking(messages)
        self.assertTrue(updated[0][1].startswith("<|think|>"))
        self.assertEqual(updated[1], ("human", "Hi"))

    def test_strip_gemma_thinking_removes_thought_block(self):
        text = "<|channel|>thought\nHidden reasoning\n<|channel|>\nFinal answer"
        self.assertEqual(strip_gemma_thinking(text), "Final answer")

    def test_normalize_content_strips_gemma_thinking_from_string(self):
        response = SimpleNamespace(
            content="<|channel|>thought\nHidden reasoning\n<|channel|>\nVisible answer"
        )
        normalized = normalize_content(response)
        self.assertEqual(normalized.content, "Visible answer")


if __name__ == "__main__":
    unittest.main()
