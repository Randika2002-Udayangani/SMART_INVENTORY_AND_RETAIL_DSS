"""Provider selection for the chatbot agent.

Tool definitions and Django business logic remain independent of the selected
LLM provider. Add future providers here without changing the tool dispatcher.
"""

from .gemini import GeminiProvider


def get_provider(system_instruction):
    return GeminiProvider(system_instruction)
