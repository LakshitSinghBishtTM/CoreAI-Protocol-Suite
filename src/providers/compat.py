"""Generic OpenAI-compatible cloud provider, used for labs without a native SDK."""

from loguru import logger

from .grok import GrokProvider


class CompatProvider(GrokProvider):
    """Same wire format as Grok; name, endpoint and default model are per instance.

    Models missing from ``config/pricing.yml`` are billed as $0 (and so are not
    budget-limited) instead of failing the request. Add prices to fix that.
    """

    def __init__(self, api_key: str, name: str, base_url: str, default_model: str = ""):
        self.name = name
        self.base_url = base_url.rstrip("/")
        if default_model:
            self.default_model = default_model
        super().__init__(api_key)

    def _bill(self, input_tokens: int, output_tokens: int, model: str) -> float:
        from coreai.pricing import PricingError  # lazy: avoids import cycle

        try:
            return super()._bill(input_tokens, output_tokens, model)
        except PricingError:
            logger.debug(f"[{self.name}] no prices configured; billing 0")
            return 0.0

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        from coreai.pricing import PricingError  # lazy: avoids import cycle

        try:
            return super().estimate_cost(input_tokens, output_tokens, model)
        except PricingError:
            return 0.0
