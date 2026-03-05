"""FinBERT-based sentiment agent for FX currency news.

Parses RSS feeds from Reuters and FXStreet (free, no API key), filters
headlines for the symbol's base/quote currencies, runs ProsusAI/finbert
for polarity classification, and returns a [-1.0, +1.0] sentiment score.

Scores are cached for 15 minutes (one score per M15 candle) to avoid
redundant model inference.

Prerequisites:
    pip install transformers feedparser

Enable via environment variable:
    USE_SENTIMENT=1  (when unset or '0', score() always returns 0.0)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Sequence

logger = logging.getLogger(__name__)

# RSS feed URLs — free, no auth required.
RSS_FEEDS: list[str] = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.fxstreet.com/rss/news",
]

# Map currency codes to keywords used in news headlines.
_CURRENCY_KEYWORDS: dict[str, list[str]] = {
    "EUR": ["euro", "eur", "ecb", "european central bank"],
    "GBP": ["pound", "gbp", "sterling", "boe", "bank of england"],
    "USD": ["dollar", "usd", "fed", "federal reserve"],
    "JPY": ["yen", "jpy", "boj", "bank of japan"],
    "AUD": ["aussie", "aud", "rba", "reserve bank of australia"],
    "CAD": ["loonie", "cad", "boc", "bank of canada"],
    "CHF": ["franc", "chf", "snb", "swiss national bank"],
}

# Label-to-score mapping for FinBERT's three-class output.
_LABEL_SCORE: dict[str, float] = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def _extract_currencies(symbol: str) -> tuple[str, str]:
    """Return (base, quote) currency codes from a 6-char FX symbol."""
    return symbol[:3].upper(), symbol[3:].upper()


def _is_relevant(text: str, base: str, quote: str) -> bool:
    """Return True if the headline likely mentions the symbol's currencies."""
    lower = text.lower()
    base_kw = _CURRENCY_KEYWORDS.get(base, [base.lower()])
    quote_kw = _CURRENCY_KEYWORDS.get(quote, [quote.lower()])
    return any(k in lower for k in base_kw) or any(k in lower for k in quote_kw)


class SentimentAgent:
    """Fetches FX news and returns a sentiment score for a given symbol.

    Scores are cached for ``cache_ttl_seconds`` (default 900 = 15 min).
    When ``USE_SENTIMENT`` env var is not '1', ``score()`` immediately
    returns 0.0 (neutral) without attempting network or model calls.
    """

    def __init__(self, cache_ttl_seconds: int = 900):
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (score, expires_at)
        self._pipeline = None  # lazy-loaded on first use

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(self, symbol: str) -> float:
        """Return sentiment score in [-1.0, +1.0] for *symbol*.

        Returns 0.0 (neutral) immediately if:
        - USE_SENTIMENT env var is not '1'
        - Required packages (transformers, feedparser) are missing
        - All RSS feeds are unreachable
        - No relevant headlines are found
        """
        if os.getenv("USE_SENTIMENT") != "1":
            return 0.0

        now = time.monotonic()
        cached_score, expires = self._cache.get(symbol, (0.0, 0.0))
        if now < expires:
            return cached_score

        result = self._compute_score(symbol)
        self._cache[symbol] = (result, now + self._cache_ttl)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_score(self, symbol: str) -> float:
        headlines = self._fetch_headlines()
        if not headlines:
            return 0.0

        base, quote = _extract_currencies(symbol)
        relevant = [h for h in headlines if _is_relevant(h, base, quote)]
        if not relevant:
            logger.debug("SentimentAgent: no relevant headlines for %s", symbol)
            return 0.0

        # Cap at 20 headlines to keep inference time under ~2 s on CPU.
        scores = self._classify(relevant[:20])
        if not scores:
            return 0.0

        return round(sum(scores) / len(scores), 4)

    def _fetch_headlines(self) -> list[str]:
        """Fetch titles from all configured RSS feeds; return empty list on failure."""
        try:
            import feedparser  # type: ignore[import]
        except ImportError:
            logger.warning("feedparser not installed — sentiment disabled. pip install feedparser")
            return []

        headlines: list[str] = []
        for url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
                headlines.extend(
                    entry.get("title", "") for entry in feed.get("entries", [])
                )
            except Exception as exc:
                logger.debug("SentimentAgent: failed to fetch %s: %s", url, exc)

        return [h for h in headlines if h]

    def _classify(self, texts: Sequence[str]) -> list[float]:
        """Run FinBERT inference on *texts*, returning per-text scores."""
        pipeline = self._get_pipeline()
        if pipeline is None:
            return []

        try:
            results = pipeline(list(texts), truncation=True, max_length=128)
            return [_LABEL_SCORE.get(r["label"].lower(), 0.0) for r in results]
        except Exception as exc:
            logger.warning("SentimentAgent: inference error: %s", exc)
            return []

    def _get_pipeline(self):
        """Lazy-load the FinBERT pipeline (CPU, single load per process)."""
        if self._pipeline is not None:
            return self._pipeline
        try:
            from transformers import pipeline as hf_pipeline  # type: ignore[import]

            logger.info("SentimentAgent: loading ProsusAI/finbert (first load may take ~30s)")
            self._pipeline = hf_pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                device=-1,  # CPU
            )
            return self._pipeline
        except ImportError:
            logger.warning(
                "transformers not installed — sentiment disabled. pip install transformers"
            )
            return None
        except Exception as exc:
            logger.warning("SentimentAgent: failed to load model: %s", exc)
            return None
