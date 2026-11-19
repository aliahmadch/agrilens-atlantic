"""SMS delivery for farmer warnings through Twilio's REST API.

Without TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER the message is
still saved in the app (status "not_sent") so the warning is never lost.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def sms_configured() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER)


def normalize_phone(value: Optional[str]) -> Optional[str]:
    """Return an E.164 number (+19025550123) or None. North American 10-digit numbers get +1."""
    if not value:
        return None
    digits = re.sub(r"[^\d+]", "", value.strip())
    if not digits.startswith("+"):
        digits = "+1" + digits if len(digits) == 10 else "+" + digits
    return digits if E164.match(digits) else None


def mask_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[:5] + "•" * max(len(value) - 8, 0) + value[-3:]


async def send_sms(to: Optional[str], body: str) -> dict[str, Any]:
    """Returns {status: sent|not_sent|failed, provider_message_id?, error?}. Never raises."""
    if not to:
        return {"status": "not_sent", "error": "No farmer phone number is set for this field."}
    if not sms_configured():
        return {"status": "not_sent", "error": "SMS is not configured (Twilio keys missing); saved in AgriLens only."}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                data={"To": to, "From": settings.TWILIO_FROM_NUMBER, "Body": body},
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            error = f"Twilio {payload.get('code') or response.status_code}: {payload.get('message') or response.text[:200]}"
            logger.warning("SMS to %s failed: %s", mask_phone(to), error)
            return {"status": "failed", "error": error}
        return {"status": "sent", "provider_message_id": payload.get("sid")}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SMS to %s failed: %s", mask_phone(to), exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
