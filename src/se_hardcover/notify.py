"""Discord webhook notifications. A no-op when no webhook is configured."""

from __future__ import annotations

import logging

import httpx

from . import USER_AGENT

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str = "") -> None:
        self.webhook_url = webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, message: str) -> None:
        """Post a plain message to Discord. Never raises — logs on failure."""
        if not self.enabled:
            logger.debug("Notifier disabled; message dropped: %s", message)
            return
        try:
            resp = httpx.post(
                self.webhook_url,
                json={"content": message[:1900]},  # Discord hard limit is 2000
                headers={"user-agent": USER_AGENT},
                timeout=15.0,
            )
            resp.raise_for_status()
        except Exception as exc:  # notifications must never break the pipeline
            logger.warning("Discord notification failed: %s", exc)

    # Convenience helpers with consistent prefixes.
    def added(self, title: str, url: str) -> None:
        self.send(f"✅ Added Standard Ebooks edition: **{title}**\n{url}")

    def queued(self, title: str, reason: str) -> None:
        self.send(f"🔎 Needs review: **{title}**\n{reason}")

    def error(self, context: str, detail: str) -> None:
        self.send(f"⚠️ Error in {context}: {detail}")
