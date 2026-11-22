"""Optional Webhook (ntfy, Discord, Slack, generic JSON) and SMTP email notifications."""

from __future__ import annotations

import logging
import smtplib
import threading
from email.message import EmailMessage

import httpx

from gphotos_synology.config import Settings

logger = logging.getLogger(__name__)


class Notifier:
    """Dispatches synchronous or non-blocking operational alerts to configured channels."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, title: str, message: str, *, level: str = "info") -> bool:
        """Synchronously send notification to configured webhook and/or SMTP recipients."""
        delivered = False
        if self.settings.webhook_url.strip():
            delivered = self._send_webhook(title, message, level=level) or delivered
        if self.settings.smtp_host.strip() and self.settings.smtp_to.strip():
            delivered = self._send_email(title, message) or delivered
        return delivered

    def send_async(self, title: str, message: str, *, level: str = "info") -> None:
        """Dispatch a notification on a background daemon thread without blocking the worker."""
        if not self.settings.webhook_url.strip() and not (
            self.settings.smtp_host.strip() and self.settings.smtp_to.strip()
        ):
            return

        def _worker() -> None:
            try:
                self.send(title, message, level=level)
            except Exception as exc:
                logger.warning("Async notification delivery failed: %s", exc)

        threading.Thread(
            target=_worker,
            name="gphotos-notifier",
            daemon=True,
        ).start()

    def _send_webhook(self, title: str, message: str, *, level: str) -> bool:
        url = self.settings.webhook_url.strip()
        wtype = self.settings.webhook_type
        from urllib.parse import urlsplit

        parsed_host = (urlsplit(url).hostname or "").lower()
        is_loopback = parsed_host in {"127.0.0.1", "localhost", "::1"}
        try:
            with httpx.Client(timeout=15.0, trust_env=not is_loopback) as client:
                if wtype == "ntfy":
                    priority = "high" if level == "error" else "default"
                    resp = client.post(
                        url,
                        content=message.encode("utf-8"),
                        headers={"Title": title, "Priority": priority},
                    )
                elif wtype == "discord":
                    resp = client.post(url, json={"content": f"**{title}**\n{message}"})
                elif wtype == "slack":
                    resp = client.post(url, json={"text": f"*{title}*\n{message}"})
                else:
                    resp = client.post(
                        url,
                        json={"title": title, "message": message, "level": level},
                    )
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Webhook notification failed: %s", exc)
            return False

    def _send_email(self, title: str, message: str) -> bool:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[GPhotos->Synology] {title}"
            msg["From"] = self.settings.smtp_from or self.settings.smtp_username or "migrator@localhost"
            msg["To"] = self.settings.smtp_to
            msg.set_content(message)

            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
                if self.settings.smtp_use_tls:
                    smtp.starttls()
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(msg)
            return True
        except Exception as exc:
            logger.warning("SMTP notification failed: %s", exc)
            return False
