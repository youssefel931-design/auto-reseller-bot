import os
import time
from datetime import datetime

import requests


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RADIUS_KM = 150
MAX_PRICE = 3000

TARGET_MODELS = [
    "vw polo",
    "vw golf",
    "opel corsa",
    "skoda fabia",
    "ford focus",
    "toyota yaris",
]

POSITIVE_KEYWORDS = [
    "tüv neu",
    "tüv bis",
    "scheckheft",
    "gepflegt",
    "1. hand",
    "2. hand",
    "unfallfrei",
    "angemeldet",
    "fahrbereit",
]

NEGATIVE_KEYWORDS = [
    "motorschaden",
    "unfall",
    "ohne tüv",
    "export",
    "bastler",
    "teileträger",
    "schlachtfest",
    "nicht fahrbereit",
    "getriebeschaden",
    "zylinderkopfdichtung",
    "wasserverlust",
    "ölverlust",
]


def require_env() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN fehlt")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID fehlt")


def send_telegram_message(message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": "false",
        },
        timeout=20,
    )
    response.raise_for_status()


def main() -> None:
    require_env()

    startup_message = (
        "Auto-Reseller-Bot gestartet.\n"
        f"Radius: {RADIUS_KM} km\n"
        f"Max Preis: {MAX_PRICE} €\n"
        f"Modelle: {', '.join(TARGET_MODELS)}"
    )

    print(startup_message)
    send_telegram_message(startup_message)

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Bot aktiv: {now}")
        time.sleep(300)


if __name__ == "__main__":
    main()
