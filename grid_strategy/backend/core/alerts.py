"""Outbound alerting — currently WhatsApp via CallMeBot.

CallMeBot is a free personal-use relay: the user messages the CallMeBot number
once to obtain an API key, then a single authenticated HTTPS GET delivers a
WhatsApp message to that same number. Perfect for a locally-run, single-operator
system — no account, no billing, no webhook server.

    GET https://api.callmebot.com/whatsapp.php?phone=<E164>&text=<urlenc>&apikey=<key>

`send_whatsapp` never raises — delivery is best-effort and failures are logged,
because an alert-send failure must not disturb the trading loop.
"""

import logging
import urllib.parse

import httpx

logger = logging.getLogger("grid.alerts")

_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


async def send_whatsapp(number: str, apikey: str, text: str) -> tuple[bool, str]:
    """Send one WhatsApp message via CallMeBot. Returns (ok, detail)."""
    number = (number or "").strip()
    apikey = (apikey or "").strip()
    if not number or not apikey:
        return False, "whatsapp number or CallMeBot API key not configured"
    params = {"phone": number, "text": text, "apikey": apikey}
    url = f"{_CALLMEBOT_URL}?{urllib.parse.urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
        ok = r.status_code < 400
        detail = f"HTTP {r.status_code}"
        if not ok:
            logger.warning("CallMeBot send failed: %s %s", r.status_code, r.text[:200])
        return ok, detail
    except Exception as e:  # noqa: BLE001 — never let an alert failure escape
        logger.warning("CallMeBot send error: %s", e)
        return False, str(e)
