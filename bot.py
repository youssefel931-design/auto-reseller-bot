import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path("/data/state.json")
REQUEST_TIMEOUT = 20
TELEGRAM_CAPTION_LIMIT = 1024

SEARCH_SOURCES = [
    {
        "key": "kleinanzeigen",
        "label": "Kleinanzeigen",
        "url": "https://www.kleinanzeigen.de/s-autos/dietzenbach/anbieter:privat/preis::3000/c216l4556r150",
    },
    {
        "key": "mobile",
        "label": "mobile.de",
        "url": "https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&s=Car&vc=Car&asl=true&cn=DE&fr=2005&gn=Dietzenbach%2C+Hessen&ll=50.009289%2C8.77697&p=%3A3000&rd=150&st=FSBO&ref=dsp",
    },
]

MAX_PRICE = 3000
MIN_YEAR = 2005
MAX_KM = 200000

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
    "scheckheftgepflegt",
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


def load_state() -> dict[str, dict[str, list[str]]]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not STATE_FILE.exists():
        return {"seen_ids": {}}

    with STATE_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        return {"seen_ids": {}}

    seen_ids = data.get("seen_ids", {})
    if isinstance(seen_ids, list):
        # Rueckwaertskompatibel: alter Stand nur fuer Kleinanzeigen
        return {"seen_ids": {"kleinanzeigen": [item for item in seen_ids if isinstance(item, str)]}}

    if not isinstance(seen_ids, dict):
        return {"seen_ids": {}}

    normalized: dict[str, list[str]] = {}
    for source_key, values in seen_ids.items():
        if isinstance(source_key, str) and isinstance(values, list):
            normalized[source_key] = [item for item in values if isinstance(item, str)]

    return {"seen_ids": normalized}


def save_state(state: dict[str, dict[str, list[str]]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def get_seen_ids(state: dict[str, dict[str, list[str]]], source_key: str) -> set[str]:
    return set(state.get("seen_ids", {}).get(source_key, []))


def set_seen_ids(state: dict[str, dict[str, list[str]]], source_key: str, seen_ids: set[str]) -> None:
    if "seen_ids" not in state or not isinstance(state["seen_ids"], dict):
        state["seen_ids"] = {}
    state["seen_ids"][source_key] = sorted(seen_ids)


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


def parse_first_registration_year(text: str) -> int | None:
    patterns = [
        r"Erstzulassung\s+([A-Za-zäöüÄÖÜ]+\s+)?(\d{4})",
        r"\bEZ\s+(\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            year_match = re.search(r"\d{4}", match.group(0))
            if year_match:
                return int(year_match.group(0))

    return None


def parse_km(text: str) -> int | None:
    patterns = [
        r"Kilometerstand\s+([\d\.\s]+)\s*km",
        r"\b([\d\.\s]{2,})\s*km\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            digits = re.sub(r"[^\d]", "", match.group(1))
            if digits:
                return int(digits)

    return None


def parse_hu(text: str) -> str:
    match = re.search(r"(HU|TÜV)\s+(bis\s+)?([A-Za-zäöüÄÖÜ]+\s+\d{4}|\d{2}/\d{4})", text, re.IGNORECASE)
    if not match:
        return ""
    return clean_text(match.group(0))


def extract_provider_type(text: str) -> str:
    lowered = text.lower()
    if "privater nutzer" in lowered or "privatanbieter" in lowered or "privat" in lowered:
        return "Privat"
    if "gewerblicher anbieter" in lowered or "gewerblich" in lowered or "händler" in lowered:
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


def extract_kleinanzeigen_listing_id(url: str) -> str | None:
    match = re.search(r"/(\d+)-216-", url)
    if not match:
        return None
    return match.group(1)


def extract_mobile_listing_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    listing_id = query.get("id", [])
    if listing_id:
        return listing_id[0]

    match = re.search(r"id=(\d+)", url)
    if match:
        return match.group(1)

    return None


def fetch_kleinanzeigen_search_results(search_url: str) -> list[dict[str, str]]:
    response = requests.get(search_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if "/s-anzeige/" not in href:
            continue

        full_url = urljoin("https://www.kleinanzeigen.de", href)
        listing_id = extract_kleinanzeigen_listing_id(full_url)
        if not listing_id or listing_id in seen_ids:
            continue

        seen_ids.add(listing_id)
        records.append({"id": listing_id, "url": full_url})

    return records


def fetch_mobile_search_results(search_url: str) -> list[dict[str, str]]:
    response = requests.get(search_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    records: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if "/fahrzeuge/details.html" not in href:
            continue

        full_url = urljoin("https://suchen.mobile.de", href)
        listing_id = extract_mobile_listing_id(full_url)
        if not listing_id or listing_id in seen_ids:
            continue

        seen_ids.add(listing_id)
        records.append({"id": listing_id, "url": full_url})

    return records


def fetch_search_results(source: dict[str, str]) -> list[dict[str, str]]:
    if source["key"] == "kleinanzeigen":
        return fetch_kleinanzeigen_search_results(source["url"])
    if source["key"] == "mobile":
        return fetch_mobile_search_results(source["url"])
    return []


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
    matched_model = model_matches(f"{title} {page_text}")

    return {
        "title": title or "Auto-Angebot",
        "image_url": image_url,
        "price": price,
        "first_registration_year": first_registration_year,
        "km": km,
        "hu": hu,
        "provider_type": provider_type,
        "page_text": page_text,
        "model": matched_model or "",
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

    km = details.get("km")
    if not isinstance(km, int):
        return False, "Keine KM erkannt"
    if km > MAX_KM:
        return False, "Zu viele KM"

    hu = str(details.get("hu", ""))
    if not hu:
        return False, "Kein TÜV/HU erkannt"

    return True, matched_model


def build_score(details: dict[str, str | int | None]) -> int:
    text = str(details.get("page_text", ""))
    score = count_positive_keywords(text)

    km = details.get("km")
    if isinstance(km, int):
        if km <= 100000:
            score += 3
        elif km <= 140000:
            score += 2
        elif km <= 180000:
            score += 1

    price = details.get("price")
    if isinstance(price, int):
        if price <= 1800:
            score += 3
        elif price <= 2300:
            score += 2
        elif price <= 2800:
            score += 1

    first_registration_year = details.get("first_registration_year")
    if isinstance(first_registration_year, int):
        if first_registration_year >= 2012:
            score += 2
        elif first_registration_year >= 2008:
            score += 1

    hu = str(details.get("hu", "")).lower()
    if "tüv neu" in text.lower() or "hu neu" in text.lower():
        score += 2
    elif hu:
        score += 1

    return score


def score_label(score: int) -> str:
    if score >= 8:
        return "sehr interessant"
    if score >= 5:
        return "interessant"
    if score >= 3:
        return "okay"
    return "eher schwach"


def price_band_label(price: int, reference_median: int) -> str:
    if reference_median <= 0:
        return "kein Vergleich"

    ratio = price / reference_median

    if ratio <= 0.75:
        return "sehr guenstig"
    if ratio <= 0.90:
        return "eher guenstig"
    if ratio <= 1.10:
        return "normal"
    return "eher teuer"


def build_market_reference(
    all_details: list[dict[str, str | int | None]],
    target_details: dict[str, str | int | None],
) -> tuple[str, int | None]:
    target_model = str(target_details.get("model", "")).strip().lower()
    target_year = target_details.get("first_registration_year")
    target_km = target_details.get("km")

    comparable_prices: list[int] = []

    for details in all_details:
        model = str(details.get("model", "")).strip().lower()
        price = details.get("price")
        year = details.get("first_registration_year")
        km = details.get("km")

        if model != target_model:
            continue
        if not isinstance(price, int):
            continue

        year_ok = True
        km_ok = True

        if isinstance(target_year, int) and isinstance(year, int):
            year_ok = abs(target_year - year) <= 3

        if isinstance(target_km, int) and isinstance(km, int):
            km_ok = abs(target_km - km) <= 50000

        if year_ok and km_ok:
            comparable_prices.append(price)

    if len(comparable_prices) < 3:
        fallback_prices = []
        for details in all_details:
            model = str(details.get("model", "")).strip().lower()
            price = details.get("price")
            if model == target_model and isinstance(price, int):
                fallback_prices.append(price)
        comparable_prices = fallback_prices

    if len(comparable_prices) < 2:
        return "kein Vergleich", None

    ref = int(median(comparable_prices))
    price = target_details.get("price")
    if not isinstance(price, int):
        return "kein Vergleich", ref

    return price_band_label(price, ref), ref


def build_message(
    source_label: str,
    url: str,
    details: dict[str, str | int | None],
    matched_model: str,
    market_label: str,
    market_reference: int | None,
) -> str:
    title = str(details.get("title", "Auto-Angebot"))
    price = details.get("price")
    first_registration_year = details.get("first_registration_year")
    km = details.get("km")
    hu = str(details.get("hu", ""))

    score = build_score(details)
    label = score_label(score)

    lines = [
        f"Neue Auto-Chance auf {source_label}",
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

    lines.append(f"Qualitaet: {label} (Score {score})")

    if market_reference is not None:
        lines.append(f"Preisvergleich: {market_label} (Vergleich ca. {market_reference} €)")
    else:
        lines.append(f"Preisvergleich: {market_label}")

    lines.append("")
    lines.append(url)

    return "\n".join(lines)


def main() -> None:
    require_env()
    state = load_state()

    startup_message = (
        "Auto-Reseller-Bot gestartet.\n"
        f"Quellen: {', '.join(source['label'] for source in SEARCH_SOURCES)}\n"
        f"Radius: 150 km\n"
        f"Max Preis: {MAX_PRICE} €\n"
        f"EZ ab: {MIN_YEAR}\n"
        f"Max KM: {MAX_KM:,}".replace(",", ".") + "\n"
        f"Modelle: {', '.join(TARGET_MODELS)}"
    )

    print(startup_message)
    send_telegram_message(startup_message)

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Neuer Durchlauf: {now}")

        try:
            all_records_by_source: dict[str, list[dict[str, str]]] = {}
            all_details_by_source: dict[str, list[dict[str, str | int | None]]] = {}
            all_fetched_details: dict[str, dict[str, dict[str, str | int | None]]] = {}

            for source in SEARCH_SOURCES:
                records = fetch_search_results(source)
                all_records_by_source[source["key"]] = records
                print(f"{source['label']}: {len(records)} Anzeigen gefunden.")

                fetched_details: dict[str, dict[str, str | int | None]] = {}
                for item in records:
                    try:
                        fetched_details[item["id"]] = fetch_listing_details(item["url"])
                    except Exception as exc:
                        print(f"Fehler beim Vorladen {source['label']} {item['url']}: {exc}")

                all_fetched_details[source["key"]] = fetched_details
                all_details_by_source[source["key"]] = list(fetched_details.values())

            for source in SEARCH_SOURCES:
                source_key = source["key"]
                source_label = source["label"]
                seen_ids = get_seen_ids(state, source_key)
                records = all_records_by_source[source_key]
                fetched_details = all_fetched_details[source_key]
                all_details = all_details_by_source[source_key]

                if not seen_ids:
                    seen_ids.update(item["id"] for item in records)
                    set_seen_ids(state, source_key, seen_ids)
                    save_state(state)
                    print(f"{source_label}: Erster Start, aktuelle Anzeigen gespeichert, nichts gesendet.")
                    continue

                new_records = [item for item in records if item["id"] not in seen_ids]

                for item in new_records:
                    url = item["url"]
                    try:
                        details = fetched_details.get(item["id"])
                        if not details:
                            details = fetch_listing_details(url)

                        is_good, reason = is_interesting_listing(details)
                        seen_ids.add(item["id"])

                        if not is_good:
                            print(f"{source_label}: Übersprungen {url} ({reason})")
                            continue

                        market_label, market_reference = build_market_reference(all_details, details)
                        message = build_message(source_label, url, details, reason, market_label, market_reference)

                        image_url = str(details.get("image_url", ""))
                        if image_url:
                            send_telegram_photo(image_url, message)
                        else:
                            send_telegram_message(message)

                        print(f"{source_label}: Gesendet {url}")

                    except Exception as exc:
                        seen_ids.add(item["id"])
                        print(f"{source_label}: Fehler bei Anzeige {url}: {exc}")

                set_seen_ids(state, source_key, seen_ids)
                save_state(state)

        except Exception as exc:
            print(f"Fehler im Durchlauf: {exc}")

        print("Warte 300 Sekunden.")
        time.sleep(300)


if __name__ == "__main__":
    main()
