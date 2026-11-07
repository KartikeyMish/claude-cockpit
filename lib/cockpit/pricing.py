"""API-equivalent cost and context-window math for Claude Code usage records.

Prices are USD per million tokens, copied from
https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-07).
Subscription users aren't billed per token; the figures show what the same
traffic would cost on the API, which is the useful number for comparing
sessions, agents and habits. Update PRICES when the pricing page changes.
"""

from __future__ import annotations

from dataclasses import dataclass

MTOK = 1_000_000
HAIKU_55_TIER_TOKENS = 100_000      # Haiku 5.5 bills prompts above this at higher rates
US_ONLY_MULTIPLIER = 1.1            # inference_geo = "us" on 4.6+ models
WARN_CONTEXT_PCT = 70
CRITICAL_CONTEXT_PCT = 85


@dataclass(frozen=True)
class Price:
    input: float
    write_5m: float
    write_1h: float
    read: float
    output: float
    fast_multiplier: float = 1.0    # fast mode premium (same factor on every category)


_OPUS_45_48 = Price(5, 6.25, 10, 0.50, 25)
_SONNET_4X = Price(3, 3.75, 6, 0.30, 15)
_OPUS_41 = Price(15, 18.75, 30, 1.50, 75)

# Longest matching prefix wins, so "claude-opus-5-5" beats "claude-opus-5".
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10, 12.50, 20, 0.25, 50),
    "claude-mythos-5-1": Price(10, 12.50, 20, 0.25, 50),
    "claude-fable-5": Price(10, 12.50, 20, 1.00, 50),
    "claude-opus-5-5": Price(4, 5, 8, 0.20, 20, fast_multiplier=2.0),
    "claude-opus-5": Price(5, 6.25, 10, 0.50, 25, fast_multiplier=2.0),
    "claude-opus-4-8": Price(5, 6.25, 10, 0.50, 25, fast_multiplier=2.0),
    "claude-opus-4-7": _OPUS_45_48,
    "claude-opus-4-6": _OPUS_45_48,
    "claude-opus-4-5": _OPUS_45_48,
    "claude-opus-4-1": _OPUS_41,
    "claude-opus-4": _OPUS_41,
    "claude-sonnet-5-5": Price(2, 2.50, 4, 0.10, 10),
    "claude-sonnet-5": Price(2, 2.50, 4, 0.20, 10),
    "claude-sonnet-4-6": _SONNET_4X,
    "claude-sonnet-4-5": _SONNET_4X,
    "claude-sonnet-4": _SONNET_4X,
    "claude-haiku-5-5": Price(0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5": Price(1, 1.25, 2, 0.10, 5),
}
HAIKU_55_LONG = Price(0.50, 0.625, 1, 0.05, 2.50)

# Models before the 4.6 generation default to a 200k window; 4.6+ have 1M.
SMALL_WINDOW_PREFIXES = ("claude-opus-4-5", "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5")
SMALL_WINDOW_EXACT = ("claude-opus-4", "claude-sonnet-4")
DEFAULT_WINDOW = 1_000_000
SMALL_WINDOW = 200_000


def price_for(model: str) -> Price | None:
    model = (model or "").lower()
    best = max((p for p in PRICES if model.startswith(p)), key=len, default=None)
    return PRICES[best] if best else None


def _num(usage: dict, key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def prompt_tokens(usage: dict) -> int:
    """Everything the model read for one request = the context size at that moment."""
    return _num(usage, "input_tokens") + _num(usage, "cache_creation_input_tokens") + _num(usage, "cache_read_input_tokens")


def request_cost(model: str, usage: object) -> float | None:
    """USD for one API response's usage block; None for unknown models."""
    if not isinstance(usage, dict):
        return 0.0
    if prompt_tokens(usage) + _num(usage, "output_tokens") == 0:
        return 0.0  # placeholders such as "<synthetic>" messages cost nothing
    price = price_for(model)
    if price is None:
        return None
    if (model or "").lower().startswith("claude-haiku-5-5") and prompt_tokens(usage) > HAIKU_55_TIER_TOKENS:
        price = HAIKU_55_LONG
    created = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write_1h = _num(created, "ephemeral_1h_input_tokens")
    write_5m = _num(created, "ephemeral_5m_input_tokens")
    if write_1h + write_5m == 0:
        write_5m = _num(usage, "cache_creation_input_tokens")
    total = (_num(usage, "input_tokens") * price.input + write_5m * price.write_5m + write_1h * price.write_1h
             + _num(usage, "cache_read_input_tokens") * price.read + _num(usage, "output_tokens") * price.output) / MTOK
    if usage.get("speed") == "fast":
        total *= price.fast_multiplier
    if usage.get("inference_geo") == "us":
        total *= US_ONLY_MULTIPLIER
    return total


def context_window(model: str, observed_tokens: int = 0) -> int:
    model = (model or "").lower()
    small = model.startswith(SMALL_WINDOW_PREFIXES) or model in SMALL_WINDOW_EXACT or any(
        model.startswith(p + "-2") for p in SMALL_WINDOW_EXACT)  # dated 4.0 ids
    window = SMALL_WINDOW if small else DEFAULT_WINDOW
    return DEFAULT_WINDOW if observed_tokens > window else window


def context_level(pct: float) -> str:
    if pct >= CRITICAL_CONTEXT_PCT:
        return "critical"
    return "warn" if pct >= WARN_CONTEXT_PCT else "ok"


def fmt_usd(amount: float | None) -> str:
    if amount is None:
        return "?"
    if amount == 0:
        return "$0"
    if amount < 0.01:
        return "<$0.01"
    return f"${amount:,.0f}" if amount >= 100 else f"${amount:.2f}"
