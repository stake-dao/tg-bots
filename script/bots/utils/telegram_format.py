"""Telegram formatting helpers for bot activity alerts."""


def format_activity_header(subject: str, chain: str, emoji: str | None = None) -> str:
    """Return a compact first line that fits better in Telegram previews."""
    prefix = f"{emoji.strip()} " if emoji else ""
    return f"{prefix}{subject.strip()} | {chain.strip()}\n"
