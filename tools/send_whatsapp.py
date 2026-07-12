"""Send a WhatsApp text message to Moemen via CallMeBot's free personal-use API.

SETUP (one-time, Moemen only -- has to be done from his own phone): add
+34 644 53 78 49 to WhatsApp contacts, send it "I allow callmebot to send me
messages", then put the APIKEY it replies with into API.env as CALLMEBOT_APIKEY
(plus CALLMEBOT_PHONE = Moemen's own WhatsApp number, intl format, e.g. +20...).
Personal-use only -- don't repurpose this for messaging anyone but Moemen.

Usage:
    python tools/send_whatsapp.py --text "..."

Prints JSON: {"status":"sent","phone":...} or {"error":...}.
"""
import argparse
import os

from _common import load_env, emit, fail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True, help="Message body")
    args = parser.parse_args()

    load_env()
    phone = os.environ.get("CALLMEBOT_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not apikey:
        fail("CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set in API.env. One-time setup: add "
             "+34 644 53 78 49 to WhatsApp contacts, send it \"I allow callmebot to send me "
             "messages\", then save the APIKEY it replies with.")
        return

    import httpx
    try:
        r = httpx.get("https://api.callmebot.com/whatsapp.php",
                       params={"phone": phone, "text": args.text, "apikey": apikey}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        fail(f"CallMeBot send failed: {e}")
        return

    emit({"status": "sent", "phone": phone})


if __name__ == "__main__":
    main()
