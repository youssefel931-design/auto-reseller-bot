import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


SEARCH_SOURCES = [
    {
        "name": "Golf 7",
        "model_key": "golf7",
        "url": "https://www.kleinanzeigen.de/s-autos/dietzenbach/golf-7/k0c216l4556r250",
    },
    {
        "name": "Golf 6",
        "model_key": "golf6",
        "url": "https://www.kleinanzeigen.de/s-autos/dietzenbach/golf-6/k0c216l4556r250",
    },
]

MODEL_LABELS = {
    "golf7": "Golf 7",
    "golf6": "Golf 6",
}

MODEL_PRIORITIES = {
    "golf7": 4,
    "golf6": 3,
}

MODEL_ALIASES = {
    "golf7": ["golf 7", "golf vii", "golf vii variant", "golf 7 variant", "golf vii kombi"],
    "golf6": ["golf 6", "golf vi", "golf vi plus", "golf 6 plus", "golf vi variant"],
}

POSITIVE_KEYWORDS = [
    "unfallfrei",
    "scheckheft",
    "scheckheftgepflegt",
    "gepflegt",
    "sehr gepflegt",
    "top zustand",
    "top-gepflegt",
    "garagenwagen",
    "nichtraucher",
    "tüv neu",
    "hu neu",
    "1. hand",
    "2. hand",
    "wenig kilometer",
    "regelmäßig gewartet",
    "lückenlos",
]

HARD_NEGATIVE_KEYWORDS = [
    "motorschaden",
    "getriebeschaden",
    "unfallwagen",
    "unfallschaden",
    "ohne tüv",
    "ohne hu",
    "nicht fahrbereit",
    "bastler",
    "export",
    "für export",
    "teileträger",
    "schlachtfest",
]

SOFT_NEGATIVE_KEYWORDS = [
    "raucherfahrzeug",
    "hagelschaden",
    "ölverlust",
    "wasserverlust",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

STATE_FILE = Path("/data/state.json")
REQUEST_TIMEOUT = 20
TELEGRAM_CAPTION_LIMIT = 1024

MAX_PRICE_ALWAYS = 7000
MAX_PRICE_VB = 8500
MAX_KM = 160000
MAX_DISTANCE_KM = 250
MIN_YEAR = 2012


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Umgebungsvariable fehlt: {name}")
    return value


def load_state() -> dict[str, list[str]]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return {}

    clean_state: dict[str, list[str]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, list):
            clean_state[key] = [item for item in value if isinstance(item, str)]
    return clean_state


def save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def extract_listing_id(url: str) -> str | None:
    match = re.search(r"/(\d+)-216-", url)
    if not match:
        return None
    return match.group(1)


def fetch_search_results(source_url: str) -> list[dict[str, str]]:
    response = requests.get(source_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = make_soup(response.text)
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
    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return None
    return int(digits)


def parse_km(text: str) -> int | None:
    patterns = [
        r"Kilometerstand\s+([\d\.\s]+)\s*km",
        r"([\d\.\s]+)\s*km\s*Kilometerstand",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            digits = re.sub(r"[^\d]", "", match.group(1))
            if digits:
                return int(digits)
    return None


def parse_year(text: str) -> int | None:
    match = re.search(r"Erstzulassung\s+([A-Za-zäöüÄÖÜ]+\s+)?(\d{4})", text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(2))


def extract_field(text: str, label: str, choices: list[str] | None = None) -> str:
    pattern = rf"{re.escape(label)}\s+([^\n\r]+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return ""

    value = clean_text(match.group(1))
    if not choices:
        return value

    lowered = value.lower()
    for choice in choices:
        if choice.lower() in lowered:
            return choice
    return value


def parse_hu(text: str) -> str:
    match = re.search(r"(HU|TÜV)\s+(bis\s+)?([A-Za-zäöüÄÖÜ]+\s+\d{4}|\d{2}/\d{4})", text, re.IGNORECASE)
    if not match:
        return ""
    return clean_text(match.group(0))


def is_vb(text: str) -> bool:
    lowered = text.lower()
    return " vb" in lowered or "verhandlungsbasis" in lowered or "vb " in lowered


def detect_model(text: str, source_model_key: str) -> str:
    lowered = text.lower()

    for model_key, aliases in MODEL_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                return model_key

    return source_model_key


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def quality_score(details: dict[str, str | int | bool | None]) -> tuple[int, list[str], list[str]]:
    text = str(details.get("page_text", ""))
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []

    model_key = str(details.get("model_key", ""))
    model_priority = MODEL_PRIORITIES.get(model_key, 0)
    score += model_priority * 2
    if model_priority:
        reasons.append(f"Modell-Prioritaet {MODEL_LABELS.get(model_key, model_key)}")

    km = details.get("km")
    if isinstance(km, int):
        if km <= 100000:
            score += 4
            reasons.append("wenig KM")
        elif km <= 130000:
            score += 3
            reasons.append("gute KM")
        elif km <= 160000:
            score += 1
            reasons.append("noch okay KM")

    year = details.get("year")
    if isinstance(year, int):
        if year >= 2016:
            score += 3
            reasons.append("juengeres Baujahr")
        elif year >= 2014:
            score += 2
            reasons.append("solides Baujahr")
        elif year >= 2012:
            score += 1

    price = details.get("price")
    vb = bool(details.get("vb"))
    if isinstance(price, int):
        if price <= 6000:
            score += 3
            reasons.append("guter Preis")
        elif price <= 7000:
            score += 2
            reasons.append("im Budget")
        elif price <= 8500 and vb:
            score += 1
            reasons.append("VB ueber Budget")

    fuel = str(details.get("fuel", "")).lower()
    if "benzin" in fuel:
        score += 3
        reasons.append("Benziner")
    elif "diesel" in fuel:
        score -= 1
        warnings.append("Diesel")

    transmission = str(details.get("transmission", "")).lower()
    if "manuell" in transmission or "schalt" in transmission:
        score += 2
        reasons.append("Schalter")

    condition = str(details.get("condition", "")).lower()
    if "beschädigt" in condition:
        score -= 4
        warnings.append("Zustand als beschädigt markiert")

    positive_hits = keyword_matches(text, POSITIVE_KEYWORDS)
    soft_negative_hits = keyword_matches(text, SOFT_NEGATIVE_KEYWORDS)

    score += len(positive_hits)
    score -= len(soft_negative_hits) * 2

    reasons.extend(positive_hits[:4])
    warnings.extend(soft_negative_hits[:3])

    return score, reasons[:6], warnings[:4]


def score_label(score: int) -> str:
    if score >= 12:
        return "sehr interessant"
    if score >= 8:
        return "interessant"
    if score >= 5:
        return "okay"
    return "eher schwach"


def fetch_listing_details(url: str, source_model_key: str) -> dict[str, str | int | bool | None]:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = make_soup(response.text)
    page_text = clean_text(soup.get_text(" ", strip=True))

    title = (
        first_meta_content(soup, [{"property": "og:title"}, {"name": "twitter:title"}])
        or (clean_text(soup.find("h1").get_text(" ", strip=True)) if soup.find("h1") else "")
        or clean_text(soup.title.get_text(" ", strip=True))
    )

    image_url = pick_best_image_url(soup, url)
    model_key = detect_model(f"{title} {page_text}", source_model_key)

    return {
        "title": title or "Auto-Anzeige",
        "image_url": image_url,
        "page_text": page_text,
        "model_key": model_key,
        "price": parse_price_eur(page_text),
        "km": parse_km(page_text),
        "year": parse_year(page_text),
        "hu": parse_hu(page_text),
        "vb": is_vb(page_text),
        "fuel": extract_field(page_text, "Kraftstoffart", ["Benzin", "Diesel", "Hybrid"]),
        "transmission": extract_field(page_text, "Getriebe", ["Manuell", "Automatik"]),
        "condition": extract_field(
            page_text,
            "Fahrzeugzustand",
            ["Unbeschädigtes Fahrzeug", "Beschädigtes Fahrzeug"],
        ),
        "owners": extract_field(page_text, "Anzahl Fahrzeughalter"),
    }


def passes_filters(details: dict[str, str | int | bool | None]) -> tuple[bool, str]:
    model_key = str(details.get("model_key", ""))
    if not model_key:
        return False, "Kein Zielmodell"

    text = str(details.get("page_text", ""))
    hard_negative_hits = keyword_matches(text, HARD_NEGATIVE_KEYWORDS)
    if hard_negative_hits:
        return False, f"Ausschlusswort: {hard_negative_hits[0]}"

    price = details.get("price")
    vb = bool(details.get("vb"))
    if not isinstance(price, int):
        return False, "Kein Preis erkannt"
    if price > MAX_PRICE_ALWAYS and not (vb and price <= MAX_PRICE_VB):
        return False, "Preis zu hoch"

    km = details.get("km")
    if not isinstance(km, int):
        return False, "Keine KM erkannt"
    if km > MAX_KM:
        return False, "Zu viele KM"

    year = details.get("year")
    if not isinstance(year, int):
        return False, "Kein Baujahr erkannt"
    if year < MIN_YEAR:
        return False, "Baujahr zu alt"

    hu = str(details.get("hu", "")).strip()
    if not hu:
        return False, "Kein HU/TUEV erkannt"

    return True, "ok"


def build_message(url: str, details: dict[str, str | int | bool | None]) -> str:
    score, reasons, warnings = quality_score(details)
    label = score_label(score)

    title = str(details.get("title", "Auto-Anzeige"))
    model_key = str(details.get("model_key", ""))
    model_label = MODEL_LABELS.get(model_key, model_key)
    price = details.get("price")
    km = details.get("km")
    year = details.get("year")
    fuel = str(details.get("fuel", ""))
    transmission = str(details.get("transmission", ""))
    hu = str(details.get("hu", ""))
    vb = bool(details.get("vb"))
    owners = str(details.get("owners", "")).strip()

    lines = [
        "Neue Auto-Chance",
        title,
        "",
        f"Modell: {model_label}",
    ]

    if isinstance(price, int):
        price_line = f"Preis: {price} €"
        if vb:
            price_line += " VB"
        lines.append(price_line)
    if isinstance(km, int):
        lines.append(f"KM: {km:,} km".replace(",", "."))
    if isinstance(year, int):
        lines.append(f"EZ: {year}")
    if fuel:
        lines.append(f"Kraftstoff: {fuel}")
    if transmission:
        lines.append(f"Getriebe: {transmission}")
    if hu:
        lines.append(hu)
    if owners:
        lines.append(f"Halter: {owners}")

    lines.append(f"Einschaetzung: {label} (Score {score})")
    if reasons:
        lines.append("Pluspunkte: " + ", ".join(reasons))
    if warnings:
        lines.append("Auffaellig: " + ", ".join(warnings))

    lines.append("")
    lines.append(url)
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message, "disable_web_page_preview": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def send_telegram_photo(token: str, chat_id: str, photo_url: str, caption: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption[:TELEGRAM_CAPTION_LIMIT],
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def get_sleep_seconds() -> int:
    hour = datetime.now().hour
    if hour >= 23 or hour < 6:
        return 1800
    return 300


def process_source(
    token: str,
    chat_id: str,
    state: dict[str, list[str]],
    source: dict[str, str],
) -> None:
    source_name = source["name"]
    source_model_key = source["model_key"]
    listings = fetch_search_results(source["url"])
    print(f"[{source_name}] {len(listings)} Anzeigen gefunden.")

    previous = set(state.get(source_name, []))
    if source_name not in state:
        print(f"[{source_name}] Erster Start: sende aktuelle passende Anzeigen.")

    new_listings = [item for item in listings if item["id"] not in previous]

    for item in new_listings:
        url = item["url"]
        try:
            details = fetch_listing_details(url, source_model_key)
            ok, reason = passes_filters(details)

            if not ok:
                print(f"[{source_name}] Uebersprungen: {url} ({reason})")
                continue

            message = build_message(url, details)
            image_url = str(details.get("image_url", "") or "")

            if image_url:
                send_telegram_photo(token, chat_id, image_url, message)
            else:
                send_telegram_message(token, chat_id, message)

            print(f"[{source_name}] Gesendet: {url}")
        except Exception as exc:
            print(f"[{source_name}] Fehler bei Anzeige {url}: {exc}")

    state[source_name] = [item["id"] for item in listings]


def main() -> None:
    token = require_env("TELEGRAM_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    state = load_state()

    print("Auto-Suchbot gestartet.")
    print(f"Budget: bis {MAX_PRICE_ALWAYS} €, oder bis {MAX_PRICE_VB} € VB")
    print(f"Radius: {MAX_DISTANCE_KM} km")
    print(f"Max KM: {MAX_KM}")
    print(f"Min Baujahr: {MIN_YEAR}")

    while True:
        cycle_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Neuer Durchlauf: {cycle_started}")

        for source in SEARCH_SOURCES:
            try:
                process_source(token, chat_id, state, source)
            except Exception as exc:
                print(f"[{source['name']}] Fehler: {exc}")

        save_state(state)
        sleep_seconds = get_sleep_seconds()
        print(f"Durchlauf beendet. Warte {sleep_seconds} Sekunden.")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
