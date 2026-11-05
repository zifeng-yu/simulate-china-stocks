"""Replaceable public market-data adapters."""

from .baostock import BaoStockProvider, ProviderError

__all__ = ["BaoStockProvider", "ProviderError"]
