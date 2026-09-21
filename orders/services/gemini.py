"""Gemini implementation of the chatbot LLM provider interface."""

import os


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
RETIRED_GEMINI_MODELS = {"gemini-2.5-flash-lite"}


class GeminiUnavailable(Exception):
    """Raised when Gemini cannot be used without exposing provider details."""


class GeminiQuotaExceeded(GeminiUnavailable):
    """Raised when Gemini rejects a request because quota is exhausted."""


class GeminiConfigurationError(GeminiUnavailable):
    """Raised for API-key, model, or request configuration failures."""


def _safe_provider_exception(exc):
    """Map SDK failures to safe, actionable messages without SDK details."""
    message = str(exc).lower()
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429 or any(term in message for term in ("quota", "resource_exhausted", "rate limit")):
        return GeminiQuotaExceeded(
            "The chatbot AI request limit has been reached. Please try again in a few minutes."
        )
    if status_code in (401, 403) or "api key" in message or "authentication" in message:
        return GeminiConfigurationError("Chatbot AI authentication is unavailable. Please contact an administrator.")
    if status_code in (400, 404) or "model" in message or "invalid argument" in message:
        return GeminiConfigurationError("The chatbot AI configuration is unavailable. Please contact an administrator.")
    return GeminiUnavailable("The chatbot AI service is temporarily unavailable. Please try again.")


class GeminiProvider:
    """Small adapter that keeps Google SDK types out of the tool dispatcher."""

    def __init__(self, system_instruction, model=None):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise GeminiUnavailable("Chatbot AI is not configured. Please contact an administrator.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiUnavailable("Chatbot AI support is not installed on this server.") from exc

        self.types = types
        self.client = genai.Client(api_key=api_key)
        configured_model = model or os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        # Existing development shells and deployment environments may still
        # provide the retired model name. Treat it as an alias so it cannot
        # make otherwise working chatbot requests fail with a provider 404.
        self.model = (
            DEFAULT_GEMINI_MODEL
            if configured_model.removeprefix("models/") in RETIRED_GEMINI_MODELS
            else configured_model
        )
        self.system_instruction = system_instruction

    def make_contents(self, history, message):
        contents = []
        for item in history or []:
            role = "model" if item.get("role") == "assistant" else "user"
            text = str(item.get("content", "")).strip()
            if text:
                contents.append(self.types.Content(role=role, parts=[self.types.Part.from_text(text=text)]))
        contents.append(self.types.Content(role="user", parts=[self.types.Part.from_text(text=message)]))
        return contents

    def generate(self, contents, declarations):
        config = self.types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            tools=[self.types.Tool(function_declarations=declarations)],
            automatic_function_calling=self.types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            return self.client.models.generate_content(model=self.model, contents=contents, config=config)
        except Exception as exc:
            raise _safe_provider_exception(exc) from exc

    @staticmethod
    def function_calls(response):
        return list(getattr(response, "function_calls", None) or [])

    @staticmethod
    def response_text(response):
        return (getattr(response, "text", None) or "").strip()

    def append_tool_results(self, contents, response, results):
        candidate = getattr(response, "candidates", None) or []
        if not candidate:
            raise GeminiUnavailable("The chatbot could not complete this request. Please try a simpler question.")
        contents.append(candidate[0].content)
        parts = [
            self.types.Part.from_function_response(
                name=call.name, response={"result": result}
            )
            for call, result in results
        ]
        contents.append(self.types.Content(role="user", parts=parts))
