"""Minimal custom provider. Register it in providers/__init__.py."""
from providers.base import BaseProvider, CompletionRequest, CompletionResponse


class EchoProvider(BaseProvider):
    name = "echo"
    default_model = "echo-1"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        text = request.messages[-1].content
        return CompletionResponse(
            content=text, model=self.default_model, provider=self.name,
            input_tokens=0, output_tokens=0, cost_usd=0.0, latency_ms=0.0,
        )

    async def stream(self, request: CompletionRequest):
        yield request.messages[-1].content
