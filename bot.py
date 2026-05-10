import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path("/data/state.json")
REQUEST_TIMEOUT = 20
TELEGRAM_CAPTION_LIMIT = 1024

SEARCH_URL = "https://www.kleinanzeigen.de/s-zu-verschenken/dietzenbach/c192l4556r50"

POSITIVE_KEYWORDS = [
    "iphone",
    "ipad",
    "macbook",
    "samsung",
    "galaxy",
    "playstation",
    "ps4",
    "ps5",
    "xbox",
    "nintendo",
    "switch",
    "monitor",
    "fernseher",
    "tv",
    "router",
    "fritzbox",
    "lautsprecher",
    "soundbar",
    "bose",
    "sony",
    "jbl",
    "dyson",
    "kärcher",
    "kaffeemaschine",
    "staubsauger",
    "werkzeug",
    "bosch",
    "makita",
    "akku",
    "fahrrad",
]

NEGATIVE_KEYWORDS = [
    "defekt",
    "bastler",
    "ohne funktion",
    "funktioniert nicht",
    "ersatzteil",
    "ersatzteile",
    "teile",
    "nur gehäuse",
    "leer karton",
    "anleitung",
    "zubehör",
    "dummy",
    "attrappe",
    "gesperrt",
    "icloud",
    "simlock",
    "schrott",
    "kaputt",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def require_env() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN fehlt")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID fehlt")


def load_state() -> dict[str, list[str]]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {"seen_ids": []}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return {"seen_ids": []}

    seen_ids = data.get("seen_ids", [])
    if not isinstance(seen_ids, list):
        seen_ids = []

    return {"seen_ids": [item for item in seen_ids if isinstance(item, str)]}


def save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def send_telegram_message(message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": "false",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def send_telegram_photo(photo_url: str, caption: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption[:TELEGRAM_CAPTION_LIMIT],
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def extract_listing_id(url: str) -> str | None:
    match = re.search(r"/(\d+)-192-", url)
    if not match:
        return None
    return match.group(1)


def fetch_search_results() -> list[dict[str, str]]:
    response = requests.get(SEARCH_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if "/s-anzeige/" not in href:
            continue

        full_url = urljoin("https://www.kleinanzeigen.de", href)
        listing_id = extract_listing_id(full_url)
        if not listing_id or listing_id in seen_ids:
            continue

        seen_ids.add(listing_id)
        records.append({"id": listing_id, "url": full_url})

    return records


def first_meta_content(soup: BeautifulSoup, attrs_list: list[dict[str, str]]) -> str | None:
    for attrs in attrs_list:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return None


def pick_best_image_url(soup: BeautifulSoup, page_url: str) -> str:
    meta_image = first_meta_content(
        soup,
        [{"property": "og:image"}, {"name": "twitter:image"}],
    )
    if meta_image:
        return urljoin(page_url, meta_image)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue

        full_src = urljoin(page_url, src)
        lowered = full_src.lower()

        if any(word in lowered for word in ["logo", "icon", "sprite", "avatar", "profile"]):
            continue

        return full_src

    return ""


def fetch_listing_details(url: str) -> dict[str, str]:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean_text(soup.get_text(" ", strip=True))

    title = (
        first_meta_content(
            soup,
            [{"property": "og:title"}, {"name": "twitter:title"}],
        )
        or (clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else "")
        or clean_text(soup.title.get_text(" ", strip=True))
    )

    image_url = pick_best_image_url(soup, url)

    return {
        "title": title or "Neue Anzeige",
        "image_url": image_url,
        "page_text": page_text,
    }


def keyword_score(text: str) -> int:
    lowered = text.lower()
    return sum(1 for keyword in POSITIVE_KEYWORDS if keyword in lowered)


def matching_keywords(text: str) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in POSITIVE_KEYWORDS if keyword in lowered][:5]


def contains_negative_keyword(text: str) -> str | None:
    lowered = text.lower()
    for keyword in NEGATIVE_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def is_interesting_listing(details: dict[str, str]) -> tuple[bool, str]:
    text = f"{details['title']} {details['page_text']}"

    negative = contains_negative_keyword(text)
    if negative:
        return False, f"Ausschlusswort: {negative}"

    score = keyword_score(text)
    if score == 0:
        return False, "Kein Treffer-Keyword"

    return True, str(score)


def build_message(url: str, details: dict[str, str]) -> str:
    title = details["title"]
    text = f"{title} {details['page_text']}"
    matches = matching_keywords(text)
    score = keyword_score(text)

    lines = [
        "Neue Zu-verschenken-Chance",
        title,
        "",
        f"Score: {score}",
    ]

    if matches:
        lines.append("Treffer: " + ", ".join(matches))

    lines.append("")
    lines.append(url)

    return "\n".join(lines)


def main() -> None:
    require_env()
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    startup_message = (
        "Kleinanzeigen-Verschenk-Bot gestartet.\n"
        "Ort: Dietzenbach\n"
        "Radius: 50 km\n"
        "Bereich: Zu verschenken"
    )

    print(startup_message)
    send_telegram_message(startup_message)

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Neuer Durchlauf: {now}")

        try:
            records = fetch_search_results()
            print(f"{len(records)} Anzeigen gefunden.")

            if not seen_ids:
                seen_ids.update(item["id"] for item in records)
                state["seen_ids"] = sorted(seen_ids)
                save_state(state)
                print("Erster Start: aktuelle Anzeigen gespeichert, nichts gesendet.")
            else:
                new_records = [item for item in records if item["id"] not in seen_ids]

                for item in new_records:
                    url = item["url"]

                    try:
                        details = fetch_listing_details(url)
                        seen_ids.add(item["id"])

                        is_good, reason = is_interesting_listing(details)
                        if not is_good:
                            print(f"Übersprungen: {url} ({reason})")
                            continue

                        message = build_message(url, details)
                        image_url = details.get("image_url", "")

                        if image_url:
                            send_telegram_photo(image_url, message)
                        else:
                            send_telegram_message(message)

                        print(f"Gesendet: {url}")

                    except Exception as exc:
                        seen_ids.add(item["id"])
                        print(f"Fehler bei Anzeige {url}: {exc}")

                state["seen_ids"] = sorted(seen_ids)
                save_state(state)

        except Exception as exc:
            print(f"Fehler im Durchlauf: {exc}")

        print("Warte 300 Sekunden.")
        time.sleep(300)


if __name__ == "__main__":
    main()
