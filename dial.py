import logging
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VIDEOSDK_TOKEN = os.getenv("VIDEOSDK_TOKEN", "").strip()
ROUTING_RULE_ID = "rr_q8y23w"
CALLER_ID = "+918065354620"
DEFAULT_TARGET = "+918117935016"

API_URL = "https://api.videosdk.live/v2/sip/call"


def dial(target: str) -> dict:
    headers = {
        "Authorization": VIDEOSDK_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "sipCallFrom": CALLER_ID,
        "sipCallTo": target,
        "routingRuleId": ROUTING_RULE_ID,
    }

    logger.info("Dialing %s via rule %s from %s...", target, ROUTING_RULE_ID, CALLER_ID)

    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        logger.error("Network error while contacting VideoSDK: %s", exc)
        raise

    if resp.status_code >= 400:
        logger.error("VideoSDK returned %s: %s", resp.status_code, resp.text)
        if resp.status_code == 401:
            logger.error("401 usually means VIDEOSDK_TOKEN is expired or malformed.")
        elif resp.status_code == 403:
            logger.error(
                "403 usually means the routing rule, gateway, or caller ID is not "
                "authorized. Verify %s is Active and bound to my_telephony_agent.",
                ROUTING_RULE_ID,
            )
        elif resp.status_code == 400:
            logger.error(
                "400 usually means a field mismatch. Check that sipCallFrom (%s) matches "
                "a number on the routing rule, and that routingRuleId is %s.",
                CALLER_ID,
                ROUTING_RULE_ID,
            )
        resp.raise_for_status()

    raw = resp.json()
    data = raw.get("data", raw)
    logger.info("Call initiated.")
    logger.info("  roomId:    %s", data.get("roomId"))
    logger.info("  callId:    %s", data.get("id") or data.get("callId") or data.get("sipCallId"))
    logger.info("  status:    %s", data.get("status"))
    logger.debug("Full response: %s", raw)
    return {
        "provider_call_id": data.get("id") or data.get("callId") or data.get("sipCallId"),
        "room_id": data.get("roomId"),
        "status": data.get("status"),
        "raw": raw,
    }


def main() -> None:
    if not VIDEOSDK_TOKEN:
        logger.error("Missing VIDEOSDK_TOKEN in environment.")
        sys.exit(1)

    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    try:
        dial(target)
    except Exception as exc:
        logger.error("Dial failed: %s", exc)
        sys.exit(3)


if __name__ == "__main__":
    main()