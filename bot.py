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

SEARCH_URL = "https://www.kleinanzeigen.de/s-autos/dietzenbach/anbieter:privat/preis::3000/c216l4556r150"

MAX_PRICE = 3000
MIN_YEAR = 2005

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
    "hu bis",
    "scheckheft",
    "gepflegt",
    "1. hand",
    "2. hand",
    "unfallfrei",
    "angemeldet",
    "fahrbereit",
    "garagenwagen",
    "nichtraucher",
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
    "defekt",
    "nur für bastler",
    "schaden",
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
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def extract_listing_id(url: str) -> str | None:
    match = re.search(r"/(\d+)-216-", url)
    if not match:
        return None
    return match.group(1)


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


def parse_price_eur(text: str) -> int | None:
    match = re.search(r"(\d[\d\.\s]*)\s*€", text)
    if not match:
        return None

    raw = match.group(1)
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None

    return int(digits)


def parse_first_registration_year(text: str) -> int | None:
    match = re.search(r"Erstzulassung\s+([A-Za-zäöüÄÖÜ]+\s+)?(\d{4})", text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(2))


def parse_km(text: str) -> int | None:
    match = re.search(r"Kilometerstand\s+([\d\.\s]+)\s*km", text, re.IGNORECASE)
    if not match:
        return None

    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return None

    return int(digits)


def parse_hu(text: str) -> str:
    match = re.search(r"(HU|TÜV)\s+bis\s+([A-Za-zäöüÄÖÜ]+\s+\d{4}|\d{2}/\d{4})", text, re.IGNORECASE)
    if not match:
        return ""
    return clean_text(match.group(0))


def extract_provider_type(text: str) -> str:
    lowered = text.lower()
    if "privater nutzer" in lowered:
        return "Privat"
    if "gewerblicher anbieter" in lowered or "gewerblich" in lowered:
        return "Gewerblich"
    return ""


def model_matches(text: str) -> str | None:
    lowered = text.lower()
    for model in TARGET_MODELS:
        if model in lowered:
            return model
    return None


def contains_negative_keyword(text: str) -> str | None:
    lowered = text.lower()
    for keyword in NEGATIVE_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


def count_positive_keywords(text: str) -> int:
    lowered = text.lower()
    count = 0
    for keyword in POSITIVE_KEYWORDS:
        if keyword in lowered:
            count += 1
    return count


def fetch_listing_details(url: str) -> dict[str, str | int | None]:
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
    price = parse_price_eur(page_text)
    first_registration_year = parse_first_registration_year(page_text)
    km = parse_km(page_text)
    hu = parse_hu(page_text)
    provider_type = extract_provider_type(page_text)

    return {
        "title": title or "Auto-Angebot",
        "image_url": image_url,
        "price": price,
        "first_registration_year": first_registration_year,
        "km": km,
        "hu": hu,
        "provider_type": provider_type,
        "page_text": page_text,
    }


def is_interesting_listing(details: dict[str, str | int | None]) -> tuple[bool, str]:
    text = str(details.get("page_text", ""))
    title = str(details.get("title", ""))
    combined = f"{title} {text}"

    matched_model = model_matches(combined)
    if not matched_model:
        return False, "Kein Zielmodell"

    negative = contains_negative_keyword(combined)
    if negative:
        return False, f"Ausschlusswort: {negative}"

    provider_type = str(details.get("provider_type", ""))
    if provider_type and provider_type != "Privat":
        return False, "Nicht privat"

    price = details.get("price")
    if not isinstance(price, int):
        return False, "Kein Preis erkannt"
    if price > MAX_PRICE:
        return False, "Preis zu hoch"

    first_registration_year = details.get("first_registration_year")
    if not isinstance(first_registration_year, int):
        return False, "Keine Erstzulassung erkannt"
    if first_registration_year < MIN_YEAR:
        return False, "Zu alt"

    hu = str(details.get("hu", ""))
    if not hu:
        return False, "Kein TÜV/HU erkannt"

    return True, matched_model


def build_score(details: dict[str, str | int | None]) -> int:
    text = str(details.get("page_text", ""))
    score = count_positive_keywords(text)

    km = details.get("km")
    if isinstance(km, int):
        if km <= 120000:
            score += 2
        elif km <= 160000:
            score += 1

    price = details.get("price")
    if isinstance(price, int):
        if price <= 2000:
            score += 2
        elif price <= 2500:
            score += 1

    return score


def build_message(url: str, details: dict[str, str | int | None], matched_model: str) -> str:
    title = str(details.get("title", "Auto-Angebot"))
    price = details.get("price")
    first_registration_year = details.get("first_registration_year")
    km = details.get("km")
    hu = str(details.get("hu", ""))
    score = build_score(details)

    lines = [
        "Neue Auto-Chance",
        title,
        "",
        f"Modell: {matched_model}",
    ]

    if isinstance(price, int):
        lines.append(f"Preis: {price} €")
    if isinstance(first_registration_year, int):
        lines.append(f"EZ: {first_registration_year}")
    if isinstance(km, int):
        lines.append(f"KM: {km:,} km".replace(",", "."))
    if hu:
        lines.append(hu)

    lines.append(f"Score: {score}")
    lines.append("")
    lines.append(url)

    return "\n".join(lines)


def main() -> None:
    require_env()
    state = load_state()

    seen_ids = set(state.get("seen_ids", []))

    startup_message = (
        "Auto-Reseller-Bot gestartet.\n"
        f"Radius: 150 km\n"
        f"Max Preis: {MAX_PRICE} €\n"
        f"EZ ab: {MIN_YEAR}\n"
        f"Modelle: {', '.join(TARGET_MODELS)}"
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
                        is_good, reason = is_interesting_listing(details)

                        seen_ids.add(item["id"])

                        if not is_good:
                            print(f"Übersprungen: {url} ({reason})")
                            continue

                        message = build_message(url, details, reason)

                        image_url = str(details.get("image_url", ""))
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
