from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import html
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

try:
    import requests
except ImportError as exc:  # pragma: no cover - startup guidance
    raise SystemExit(
        "Missing dependency: requests. Install it with: python -m pip install -r requirements.txt"
    ) from exc


APP_NAME = "home-scanner"
DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_STATE_PATH = Path("seen.json")
DEFAULT_TRANSLATION_CACHE_PATH = Path("translations.json")
OLX_ROOT = "https://www.olx.pl"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,tr;q=0.8,en;q=0.7",
}

ROOM_VALUES = {
    "1": "one",
    "one": "one",
    "studio": "one",
    "kawalerka": "one",
    "2": "two",
    "two": "two",
    "3": "three",
    "three": "three",
    "4": "four",
    "four": "four",
    "4+": "four",
}

CITY_OPTIONS = [
    {"slug": "krakow", "name": "Kraków", "city_id": 8959, "region_id": 4, "lat": 50.07567, "lon": 19.93084},
    {"slug": "warszawa", "name": "Warszawa", "city_id": 17871, "region_id": 2, "lat": 52.23614, "lon": 21.00817},
    {"slug": "wroclaw", "name": "Wrocław", "city_id": 19701, "region_id": 3, "lat": 51.10195, "lon": 17.03667},
    {"slug": "poznan", "name": "Poznań", "city_id": 13983, "region_id": 1, "lat": 52.40916, "lon": 16.89856},
    {"slug": "gdansk", "name": "Gdańsk", "city_id": 5659, "region_id": 5, "lat": 54.37156, "lon": 18.62303},
    {"slug": "lodz", "name": "Łódź", "city_id": 10609, "region_id": 7, "lat": 51.75949, "lon": 19.4318},
    {"slug": "katowice", "name": "Katowice", "city_id": 7691, "region_id": 6, "lat": 50.26288, "lon": 19.02276},
    {"slug": "lublin", "name": "Lublin", "city_id": 10119, "region_id": 8, "lat": 51.23955, "lon": 22.55257},
]

CITY_BY_SLUG = {city["slug"]: city for city in CITY_OPTIONS}

CATEGORY_OPTIONS = {
    "home": {
        "label": "Home",
        "category_path": "nieruchomosci/mieszkania/wynajem",
        "default_sort": "known_total:asc",
    },
    "car": {
        "label": "Car",
        "category_path": "motoryzacja/samochody",
        "default_sort": "filter_float_price:asc",
    },
}
CATEGORY_BY_PATH = {item["category_path"]: key for key, item in CATEGORY_OPTIONS.items()}

_TRANSLATION_CACHE: dict[str, str] | None = None
_TRANSLATION_CACHE_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Listing:
    id: str
    title: str
    url: str
    price_value: float | None
    price_label: str
    rent_value: float | None
    area_m2: float | None
    rooms_key: str | None
    rooms_label: str
    furniture_key: str | None
    furniture_label: str
    location: str
    district: str
    lat: float | None
    lon: float | None
    map_radius: float | None
    created_time: str
    refresh_time: str
    description: str
    photos: list[str]
    cost_items: list[dict[str, Any]]
    details: list[str]
    has_photo: bool

    @property
    def total_known_cost(self) -> float | None:
        base = self.price_value
        if base is None:
            return None
        explicit_totals = [
            float(item["amount_value"])
            for item in self.cost_items
            if item.get("kind") == "Total" and item.get("amount_value") is not None
        ]
        explicit_totals = [amount for amount in explicit_totals if amount >= base]
        if explicit_totals:
            return min(explicit_totals)
        total = base
        if self.rent_value is not None:
            total += self.rent_value
        return total


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return default or {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Create one with: python {Path(__file__).name} init --config {path}"
        )
    config = load_json_file(path)
    if not isinstance(config, dict):
        raise SystemExit("Config file must contain a JSON object.")
    return config


def write_default_config(path: Path, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise SystemExit(f"Config already exists: {path}. Use --force to overwrite it.")
    example = load_json_file(Path(__file__).with_name("config.example.json"))
    save_json_file(path, example)
    print(f"Created config: {path}")


def configure_interactively(path: Path) -> None:
    config = load_json_file(path) if path.exists() else load_json_file(Path(__file__).with_name("config.example.json"))
    if not config:
        config = {}

    print("OLX filter setup")
    print("Press Enter to keep the current value. Use '-' to clear it.")
    print("")

    current_url = str(config.get("search_url") or "")
    search_url = prompt_text("OLX filter URL", current_url)

    if search_url:
        config["search_url"] = search_url
        config["local_filters"] = empty_local_filters()
        print("URL mode is enabled. Local filters were cleared to avoid hidden conflicts.")
        configure_extra_local_filters(config)
    else:
        config["search_url"] = ""
        configure_olx_filters(config)
        sync_local_filters_from_olx(config)
        configure_extra_local_filters(config)

    config["max_pages"] = prompt_int("How many pages should be scanned", config.get("max_pages"), minimum=1) or 1
    config["scan_interval_minutes"] = (
        prompt_float("Scan interval in minutes", config.get("scan_interval_minutes"), minimum=1) or 10
    )

    save_json_file(path, config)
    print("")
    print(f"Config saved: {path}")
    print("Current OLX URL:")
    print(build_search_url(config))


def configure_olx_filters(config: dict[str, Any]) -> None:
    filters = config.setdefault("olx_filters", {})
    filters["category_path"] = "nieruchomosci/mieszkania/wynajem"
    filters["city_slug"] = prompt_text("City slug (for example: warszawa, krakow, wroclaw)", filters.get("city_slug") or "warszawa")
    filters["query"] = prompt_text("Search query", filters.get("query") or "")
    filters["sort"] = prompt_choice(
        "Sort order",
        filters.get("sort") or "created_at:desc",
        {"1": "created_at:desc", "2": "filter_float_price:asc", "3": "filter_float_price:desc"},
        "1=Newest listings, 2=Cheapest first, 3=Most expensive first",
    )
    filters["owner_type"] = prompt_choice(
        "Seller type",
        filters.get("owner_type") or "private",
        {"1": "private", "2": "business", "3": None},
        "1=Private, 2=Business, 3=All",
    )
    filters["price_from"] = prompt_int("Minimum price PLN", filters.get("price_from"), minimum=0)
    filters["price_to"] = prompt_int("Maximum price PLN", filters.get("price_to"), minimum=0)
    filters["area_from"] = prompt_float("Minimum m2", filters.get("area_from"), minimum=0)
    filters["area_to"] = prompt_float("Maximum m2", filters.get("area_to"), minimum=0)
    filters["rooms"] = prompt_rooms("Room count (for example: 1,2 or 2,3)", filters.get("rooms"))
    filters["district_id"] = prompt_int("OLX district_id (leave blank if unknown)", filters.get("district_id"), minimum=0)
    filters["distance_km"] = prompt_int("Distance in km (leave blank if unknown)", filters.get("distance_km"), minimum=0)
    filters["only_with_photo"] = prompt_bool("Only listings with photos", filters.get("only_with_photo", True))


def configure_extra_local_filters(config: dict[str, Any]) -> None:
    filters = config.setdefault("local_filters", empty_local_filters())
    print("")
    print("Extra local filters")
    print("These are applied locally after OLX returns the results.")
    filters["districts_any"] = prompt_csv("District name filter (for example: Mokotów,Wola)", filters.get("districts_any"))
    filters["keywords_any"] = prompt_csv("Match at least one of these keywords", filters.get("keywords_any"))
    filters["keywords_all"] = prompt_csv("Match all of these keywords", filters.get("keywords_all"))
    filters["exclude_keywords"] = prompt_csv("Exclude listings containing these keywords", filters.get("exclude_keywords"))
    filters["max_total_known_cost"] = prompt_int(
        "Known total cost limit PLN (rent + administrative fees)",
        filters.get("max_total_known_cost"),
        minimum=0,
    )


def sync_local_filters_from_olx(config: dict[str, Any]) -> None:
    olx_filters = config.get("olx_filters") or {}
    local_filters = empty_local_filters()
    local_filters["min_price"] = olx_filters.get("price_from")
    local_filters["max_price"] = olx_filters.get("price_to")
    local_filters["min_area_m2"] = olx_filters.get("area_from")
    local_filters["max_area_m2"] = olx_filters.get("area_to")
    local_filters["rooms"] = olx_filters.get("rooms") or []
    local_filters["furniture"] = olx_filters.get("furniture") or []
    local_filters["require_photo"] = bool(olx_filters.get("only_with_photo"))
    config["local_filters"] = local_filters


def empty_local_filters() -> dict[str, Any]:
    return {
        "category": "home",
        "min_price": None,
        "max_price": None,
        "min_area_m2": None,
        "max_area_m2": None,
        "rooms": [],
        "furniture": [],
        "districts_any": [],
        "keywords_any": [],
        "keywords_all": [],
        "exclude_keywords": [],
        "require_photo": False,
        "min_rent": None,
        "max_rent": None,
        "min_total_known_cost": None,
        "max_total_known_cost": None,
        "center_lat": None,
        "center_lon": None,
        "radius_km": None,
        "apply_total_limit": False,
        "apply_radius_filter": False,
    }


def prompt_text(label: str, current: Any = "") -> str:
    current_text = "" if current is None else str(current)
    answer = input(format_prompt(label, current_text)).strip()
    if answer == "-":
        return ""
    return answer if answer else current_text


def prompt_int(label: str, current: Any = None, minimum: int | None = None) -> int | None:
    while True:
        value = prompt_text(label, "" if current is None else current)
        if value == "":
            return None
        try:
            parsed = int(float(value.replace(",", ".")))
        except ValueError:
            print("Enter a number.")
            current = value
            continue
        if minimum is not None and parsed < minimum:
            print(f"Minimum value is {minimum}.")
            current = value
            continue
        return parsed


def prompt_float(label: str, current: Any = None, minimum: float | None = None) -> float | None:
    while True:
        value = prompt_text(label, "" if current is None else current)
        if value == "":
            return None
        try:
            parsed = float(value.replace(",", "."))
        except ValueError:
            print("Enter a number.")
            current = value
            continue
        if minimum is not None and parsed < minimum:
            print(f"Minimum value is {minimum:g}.")
            current = value
            continue
        return parsed


def prompt_bool(label: str, current: Any = False) -> bool:
    current_text = "y" if current else "n"
    while True:
        answer = prompt_text(f"{label} (y/n)", current_text).casefold()
        if answer in {"y", "yes", "true", "1"}:
            return True
        if answer in {"n", "no", "false", "0"}:
            return False
        print("Enter 'y' or 'n'.")


def prompt_choice(label: str, current: Any, choices: dict[str, Any], help_text: str) -> Any:
    reverse = {value: key for key, value in choices.items()}
    current_key = reverse.get(current, "1")
    while True:
        answer = prompt_text(f"{label} ({help_text})", current_key)
        if answer in choices:
            return choices[answer]
        print(f"Valid options: {', '.join(choices)}")


def prompt_rooms(label: str, current: Any) -> list[str]:
    current_rooms = ",".join(as_list(current))
    while True:
        answer = prompt_text(label, current_rooms)
        if not answer:
            return []
        rooms = []
        invalid = []
        for item in answer.split(","):
            room = normalize_room(item)
            if room:
                rooms.append(room)
            else:
                invalid.append(item.strip())
        if invalid:
            print(f"Invalid room value: {', '.join(invalid)}. Use 1, 2, 3, or 4.")
            current_rooms = answer
            continue
        return rooms


def prompt_csv(label: str, current: Any) -> list[str]:
    current_text = ",".join(str(item) for item in as_list(current))
    answer = prompt_text(label, current_text)
    if not answer:
        return []
    return [item.strip() for item in answer.split(",") if item.strip()]


def format_prompt(label: str, current: str) -> str:
    if current:
        return f"{label} [{current}]: "
    return f"{label}: "


def make_session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    user_agent = config.get("user_agent")
    if user_agent:
        session.headers["User-Agent"] = str(user_agent)
    return session


def build_search_url(config: dict[str, Any]) -> str:
    search_url = str(config.get("search_url") or "").strip()
    if search_url:
        return search_url

    filters = config.get("olx_filters") or {}
    category = normalize_category((config.get("ui") or {}).get("category") or filters.get("category_path"))
    category_path = category_path_for(category).strip("/")
    city_slug = str(filters.get("city_slug") or "krakow").strip("/")
    query_text = str(filters.get("query") or "").strip()

    path = f"{category_path}/{city_slug}/"
    if query_text:
        path = f"{category_path}/{city_slug}/q-{quote(query_text)}/"

    params: list[tuple[str, str]] = []
    add_param(params, "search[order]", filters.get("sort") or "filter_float_price:asc")
    add_param(params, "search[private_business]", filters.get("owner_type"))
    add_param(params, "search[filter_float_price:from]", filters.get("price_from"))
    add_param(params, "search[filter_float_price:to]", filters.get("price_to"))
    if category == "home":
        add_param(params, "search[filter_float_m:from]", filters.get("area_from"))
        add_param(params, "search[filter_float_m:to]", filters.get("area_to"))
    if category == "car":
        add_param(params, "search[filter_float_year:from]", filters.get("year_from"))
        add_param(params, "search[filter_float_year:to]", filters.get("year_to"))
        add_param(params, "search[filter_float_milage:from]", filters.get("mileage_from"))
        add_param(params, "search[filter_float_milage:to]", filters.get("mileage_to"))
    add_param(params, "search[district_id]", filters.get("district_id"))
    add_param(params, "search[dist]", filters.get("distance_km"))

    if filters.get("only_with_photo"):
        add_param(params, "search[photos]", "1")

    if category == "home":
        for index, room in enumerate(as_list(filters.get("rooms"))):
            room_value = normalize_room(room)
            if room_value:
                params.append((f"search[filter_enum_rooms][{index}]", room_value))

        for index, furniture in enumerate(as_list(filters.get("furniture"))):
            furniture_value = normalize_furniture(furniture)
            if furniture_value:
                params.append((f"search[filter_enum_furniture][{index}]", furniture_value))

    query = urlencode(params)
    return f"{OLX_ROOT}/{path}" + (f"?{query}" if query else "")


def add_param(params: list[tuple[str, str]], name: str, value: Any) -> None:
    if value is None or value == "":
        return
    params.append((name, str(value)))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_room(value: Any) -> str | None:
    key = str(value).strip().lower()
    return ROOM_VALUES.get(key)


def normalize_furniture(value: Any) -> str | None:
    key = str(value).strip().casefold()
    if key in {"yes", "tak", "furnished", "1"}:
        return "yes"
    if key in {"no", "nie", "unfurnished", "0"}:
        return "no"
    return None


def normalize_category(value: Any) -> str:
    key = str(value or "").strip().casefold()
    if key in CATEGORY_OPTIONS:
        return key
    path = key.strip("/")
    return CATEGORY_BY_PATH.get(path, "home")


def category_path_for(category: str) -> str:
    return str(CATEGORY_OPTIONS.get(category, CATEGORY_OPTIONS["home"])["category_path"])


def bootstrap_api_url(session: requests.Session, search_url: str) -> str:
    response = session.get(search_url, timeout=30)
    response.raise_for_status()
    state = extract_prerendered_state(response.text)
    listing = (state.get("listing") or {}).get("listing") or {}

    links = listing.get("links") or {}
    self_link = links.get("self")
    if isinstance(self_link, str) and self_link:
        return urljoin(OLX_ROOT, self_link)

    params = listing.get("params") or {}
    if not params:
        raise RuntimeError("Could not find OLX API parameters in the search page.")
    return f"{OLX_ROOT}/api/v1/offers?{urlencode(params, doseq=True)}"


def extract_prerendered_state(page_html: str) -> dict[str, Any]:
    match = re.search(
        r'window\.__PRERENDERED_STATE__\s*=\s*"(?P<state>(?:\\.|[^"\\])*)";',
        page_html,
        flags=re.DOTALL,
    )
    if not match:
        raise RuntimeError("Could not find OLX prerendered state in the page.")
    decoded = json.loads(f'"{match.group("state")}"')
    return json.loads(decoded)


def fetch_api_pages(
    session: requests.Session,
    api_url: str,
    max_pages: int,
    request_delay_seconds: float,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    next_url: str | None = api_url
    page_number = 0

    while next_url and page_number < max_pages:
        response = session.get(next_url, timeout=30)
        if response.status_code == 429:
            raise RuntimeError("OLX returned HTTP 429 rate limit. Increase scan interval/delay.")
        response.raise_for_status()
        data = response.json()
        pages.append(data)
        page_number += 1
        next_url = extract_next_link(data)
        if next_url and page_number < max_pages:
            time.sleep(max(0.0, request_delay_seconds))

    return pages


def extract_next_link(api_response: dict[str, Any]) -> str | None:
    links = api_response.get("links") or {}
    next_link = links.get("next")
    if isinstance(next_link, dict):
        href = next_link.get("href")
    else:
        href = next_link
    if not href:
        return None
    return urljoin(OLX_ROOT, str(href))


def listings_from_pages(pages: list[dict[str, Any]]) -> list[Listing]:
    listings: list[Listing] = []
    seen: set[str] = set()
    for page in pages:
        for ad in page.get("data") or []:
            listing = listing_from_ad(ad)
            if listing.id in seen:
                continue
            seen.add(listing.id)
            listings.append(listing)
    return listings


def listing_from_ad(ad: dict[str, Any]) -> Listing:
    params = params_by_key(ad.get("params") or [])
    price = params.get("price") or {}
    rent = params.get("rent") or {}
    area = params.get("m") or {}
    rooms = params.get("rooms") or {}
    furniture = params.get("furniture") or {}
    map_data = ad.get("map") or {}
    location = format_location(ad.get("location") or {})
    district = extract_district(ad.get("location") or {})
    description = strip_html(str(ad.get("description") or ""))
    photos = photo_urls_from_ad(ad)

    cost_items = extract_cost_items(description, params)
    return Listing(
        id=str(ad.get("id") or ad.get("url") or ""),
        title=str(ad.get("title") or "").strip(),
        url=str(ad.get("url") or "").strip(),
        price_value=parse_number((price.get("value") or {}).get("value")),
        price_label=str((price.get("value") or {}).get("label") or ""),
        rent_value=parse_number((rent.get("value") or {}).get("key")),
        area_m2=parse_number((area.get("value") or {}).get("key")),
        rooms_key=string_or_none((rooms.get("value") or {}).get("key")),
        rooms_label=str((rooms.get("value") or {}).get("label") or ""),
        furniture_key=string_or_none((furniture.get("value") or {}).get("key")),
        furniture_label=str((furniture.get("value") or {}).get("label") or ""),
        location=location,
        district=district,
        lat=parse_number(map_data.get("lat")),
        lon=parse_number(map_data.get("lon")),
        map_radius=parse_number(map_data.get("radius")),
        created_time=str(ad.get("created_time") or ""),
        refresh_time=str(ad.get("last_refresh_time") or ad.get("pushup_time") or ""),
        description=description,
        photos=photos,
        cost_items=cost_items,
        details=listing_details(params),
        has_photo=bool(photos),
    )


def listing_details(params: dict[str, dict[str, Any]]) -> list[str]:
    details: list[str] = []
    for key in ("year", "milage", "petrol", "transmission", "car_body", "enginesize", "enginepower", "condition"):
        value = params.get(key) or {}
        label = str((value.get("value") or {}).get("label") or "").strip()
        if label and label not in details:
            details.append(label)
    return details


def photo_urls_from_ad(ad: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for photo in ad.get("photos") or []:
        link = str(photo.get("link") or "").strip()
        if not link:
            continue
        link = link.replace("{width}", "640").replace("{height}", "480")
        urls.append(link)
    return urls


COST_KEYWORDS = (
    "czynsz",
    "opłat",
    "oplat",
    "media",
    "prąd",
    "prad",
    "gaz",
    "woda",
    "ogrzew",
    "internet",
    "śmieci",
    "smieci",
    "energia",
    "rachunk",
    "kaucj",
    "administr",
    "dodatk",
    "faktur",
    "billing",
    "bills",
    "utilities",
    "included",
    "inclusive",
    "w cenie",
    "wliczon",
)


def extract_cost_items(description: str, params: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rent = params.get("rent") or {}
    rent_label = str((rent.get("value") or {}).get("label") or "").strip()
    rent_value = parse_number((rent.get("value") or {}).get("key"))
    if rent_label:
        items.append({
            "kind": "Czynsz",
            "amounts": [rent_label],
            "amount_value": None,
            "recurrence": "monthly",
            "text": f"Czynsz dodatkowo: {rent_label}",
        })

    for segment in cost_segments(description):
        normalized = normalize_text(segment)
        if not any(keyword in normalized for keyword in COST_KEYWORDS):
            continue
        amounts = extract_amount_labels(segment)
        if not amounts and not any(keyword in normalized for keyword in ("w cenie", "wliczon", "included", "inclusive")):
            continue
        kind = infer_cost_kind(normalized)
        amount_values = [parse_number(amount) for amount in amounts]
        amount_values = [amount for amount in amount_values if amount is not None]
        item = {
            "kind": kind,
            "amounts": amounts,
            "amount_value": amount_values[0] if amount_values and kind != "Deposit" else None,
            "recurrence": infer_recurrence(normalized, kind),
            "text": shorten(segment, 220),
        }
        if item not in items:
            items.append(item)
        if len(items) >= 8:
            break

    return items


def cost_segments(description: str) -> list[str]:
    pieces: list[str] = []
    for line in re.split(r"[\n\r]+", description):
        for sentence in re.split(r"(?<=[.!?])\s+|;\s+", line):
            sentence = re.sub(r"\s+", " ", sentence).strip(" -•\t")
            if sentence:
                pieces.append(sentence)
    return pieces


def extract_amount_labels(text: str) -> list[str]:
    amounts = []
    pattern = re.compile(r"(?<!\d)(\d[\d\s.,]{0,10})\s*(zł|zl|pln|eur|€)", flags=re.I)
    for value, currency in pattern.findall(text):
        normalized_value = re.sub(r"\s+", " ", value).strip()
        label = f"{normalized_value} {currency}"
        if label not in amounts:
            amounts.append(label)
    return amounts


def infer_cost_kind(normalized_text: str) -> str:
    if any(key in normalized_text for key in ("całość", "calosc", "łącznie", "lacznie", "razem", "total")):
        return "Total"
    if "kaucj" in normalized_text:
        return "Deposit"
    if "czynsz" in normalized_text or "administr" in normalized_text:
        return "Administrative fee"
    if any(key in normalized_text for key in ("media", "prąd", "prad", "gaz", "woda", "ogrzew", "internet", "śmieci", "smieci", "rachunk", "faktur", "bills", "utilities")):
        return "Utilities / bills"
    if any(key in normalized_text for key in ("w cenie", "wliczon", "included", "inclusive")):
        return "Included"
    return "Extra fee"


def infer_recurrence(normalized_text: str, kind: str) -> str:
    if kind == "Deposit":
        return "one_time"
    if any(key in normalized_text for key in ("mies", "mc", "msc", "monthly", "per month", "co miesiąc")):
        return "monthly"
    if kind in {"Administrative fee", "Utilities / bills", "Extra fee"}:
        return "monthly"
    return "unknown"


def shorten(text: str, max_length: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def params_by_key(params: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for param in params:
        key = param.get("key")
        if key:
            output[str(key)] = param
    return output


def parse_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace("\xa0", " ")
    match = re.search(r"-?\d[\d\s.,]*", text)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    has_comma = "," in number
    has_dot = "." in number
    if has_comma and has_dot:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif has_comma:
        groups = number.split(",")
        if len(groups) > 1 and len(groups[-1]) == 3 and all(len(group) == 3 for group in groups[1:]):
            number = "".join(groups)
        else:
            number = number.replace(",", ".")
    elif has_dot:
        groups = number.split(".")
        if len(groups) > 1 and len(groups[-1]) == 3 and all(len(group) == 3 for group in groups[1:]):
            number = "".join(groups)
    try:
        return float(number)
    except ValueError:
        return None


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def format_location(location: dict[str, Any]) -> str:
    parts = []
    for key in ("city", "district", "region"):
        value = location.get(key)
        if isinstance(value, dict) and value.get("name"):
            parts.append(str(value["name"]))
    return ", ".join(parts)


def extract_district(location: dict[str, Any]) -> str:
    district = location.get("district")
    if isinstance(district, dict):
        return str(district.get("name") or "")
    return ""


def apply_local_filters(listings: list[Listing], config: dict[str, Any]) -> list[Listing]:
    filters = config.get("local_filters") or {}
    output: list[Listing] = []
    for listing in listings:
        if not listing_matches(listing, filters):
            continue
        output.append(listing)
    return output


def listing_matches(listing: Listing, filters: dict[str, Any]) -> bool:
    category = normalize_category(filters.get("category"))
    if filters.get("require_photo") and not listing.has_photo:
        return False

    if not number_in_range(listing.price_value, filters.get("min_price"), filters.get("max_price")):
        return False
    if category == "home":
        if not number_in_range(listing.area_m2, filters.get("min_area_m2"), filters.get("max_area_m2")):
            return False
        if not number_in_range(listing.rent_value, filters.get("min_rent"), filters.get("max_rent")):
            return False
        if filters.get("apply_total_limit"):
            if not number_in_range(
                listing.total_known_cost,
                filters.get("min_total_known_cost"),
                filters.get("max_total_known_cost"),
            ):
                return False

        allowed_rooms = {normalize_room(room) for room in as_list(filters.get("rooms"))}
        allowed_rooms.discard(None)
        if allowed_rooms and listing.rooms_key not in allowed_rooms:
            return False

        allowed_furniture = {normalize_furniture(item) for item in as_list(filters.get("furniture"))}
        allowed_furniture.discard(None)
        if allowed_furniture and listing.furniture_key not in allowed_furniture:
            return False

    center_lat = parse_number(filters.get("center_lat"))
    center_lon = parse_number(filters.get("center_lon"))
    radius_km = parse_number(filters.get("radius_km"))
    if filters.get("apply_radius_filter") and center_lat is not None and center_lon is not None and radius_km is not None and radius_km > 0:
        if listing.lat is None or listing.lon is None:
            return False
        if haversine_km(center_lat, center_lon, listing.lat, listing.lon) > radius_km:
            return False

    haystack = normalize_text(
        " ".join([listing.title, listing.description, listing.location, listing.rooms_label, " ".join(listing.details)])
    )
    if not text_filter_matches(haystack, filters):
        return False

    districts = [normalize_text(item) for item in as_list(filters.get("districts_any")) if item]
    if districts and not any(district in normalize_text(listing.location) for district in districts):
        return False

    return True


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def number_in_range(value: float | None, minimum: Any, maximum: Any) -> bool:
    min_number = parse_number(minimum)
    max_number = parse_number(maximum)
    if min_number is None and max_number is None:
        return True
    if value is None:
        return False
    if min_number is not None and value < min_number:
        return False
    if max_number is not None and value > max_number:
        return False
    return True


def text_filter_matches(haystack: str, filters: dict[str, Any]) -> bool:
    any_terms = [normalize_text(term) for term in as_list(filters.get("keywords_any")) if term]
    all_terms = [normalize_text(term) for term in as_list(filters.get("keywords_all")) if term]
    excluded_terms = [normalize_text(term) for term in as_list(filters.get("exclude_keywords")) if term]

    if any_terms and not any(term in haystack for term in any_terms):
        return False
    if all_terms and not all(term in haystack for term in all_terms):
        return False
    if excluded_terms and any(term in haystack for term in excluded_terms):
        return False
    return True


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def search_key(search_url: str, config: dict[str, Any]) -> str:
    payload = {
        "search_url": search_url,
        "local_filters": config.get("local_filters") or {},
        "max_pages": config.get("max_pages"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def get_seen_bucket(state: dict[str, Any], key: str) -> dict[str, Any]:
    searches = state.setdefault("searches", {})
    bucket = searches.setdefault(key, {})
    bucket.setdefault("seen", {})
    return bucket


def prune_seen(bucket: dict[str, Any], ttl_days: int) -> None:
    if ttl_days <= 0:
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ttl_days)
    seen = bucket.get("seen") or {}
    for listing_id, payload in list(seen.items()):
        first_seen = str((payload or {}).get("first_seen") or "")
        try:
            first_seen_dt = dt.datetime.fromisoformat(first_seen)
        except ValueError:
            continue
        if first_seen_dt < cutoff:
            seen.pop(listing_id, None)


def update_seen(bucket: dict[str, Any], listings: list[Listing]) -> list[Listing]:
    seen = bucket.setdefault("seen", {})
    new_items: list[Listing] = []
    timestamp = now_iso()
    for listing in listings:
        if listing.id not in seen:
            new_items.append(listing)
            seen[listing.id] = {
                "first_seen": timestamp,
                "title": listing.title,
                "url": listing.url,
            }
    bucket["last_scan"] = timestamp
    return new_items


def scan_once(
    config: dict[str, Any],
    state_path: Path,
    notify_current: bool = False,
    dry_run: bool = False,
    show_all_when_no_new: bool = True,
) -> tuple[list[Listing], list[Listing]]:
    search_url, api_url, listings = fetch_matching_listings(config)

    state = load_json_file(state_path, default={"searches": {}})
    key = search_key(search_url, config)
    bucket = get_seen_bucket(state, key)
    prune_seen(bucket, int(config.get("seen_ttl_days") or 60))

    first_scan = not bucket.get("seen")
    new_items = update_seen(bucket, listings)
    if first_scan and not (config.get("first_run_notify") or notify_current):
        new_items = []
    if notify_current:
        new_items = listings

    if not dry_run:
        save_json_file(state_path, state)

    print_scan_summary(
        search_url,
        api_url,
        listings,
        new_items,
        first_scan,
        dry_run=dry_run,
        show_all_when_no_new=show_all_when_no_new,
    )

    if new_items and dry_run:
        print("Dry run: notification skipped.")
    elif new_items:
        notify(new_items, config)

    return listings, new_items


def fetch_matching_listings(config: dict[str, Any]) -> tuple[str, str, list[Listing]]:
    search_url = build_search_url(config)
    session = make_session(config)
    api_url = bootstrap_api_url(session, search_url)
    max_pages = int(config.get("max_pages") or 1)
    request_delay_seconds = float(config.get("request_delay_seconds") or 2)

    pages = fetch_api_pages(session, api_url, max_pages=max_pages, request_delay_seconds=request_delay_seconds)
    listings = apply_local_filters(listings_from_pages(pages), config)
    listings = sort_listings(listings, config)
    return search_url, api_url, listings


def sort_listings(listings: list[Listing], config: dict[str, Any]) -> list[Listing]:
    sort_mode = str((config.get("ui") or {}).get("sort_mode") or (config.get("olx_filters") or {}).get("sort") or "")
    category = normalize_category((config.get("ui") or {}).get("category") or (config.get("olx_filters") or {}).get("category_path"))
    if category == "home" and sort_mode == "known_total:asc":
        return sorted(listings, key=lambda item: (item.total_known_cost is None, item.total_known_cost or 0, item.price_value or 0))
    if category == "home" and sort_mode == "known_total:desc":
        return sorted(listings, key=lambda item: (item.total_known_cost is None, -(item.total_known_cost or 0), -(item.price_value or 0)))
    return listings


def scan_for_ui(config: dict[str, Any], state_path: Path, hide_seen: bool) -> dict[str, Any]:
    search_url, api_url, listings = fetch_matching_listings(config)
    category = normalize_category((config.get("ui") or {}).get("category") or (config.get("olx_filters") or {}).get("category_path"))

    state = load_json_file(state_path, default={"searches": {}})
    key = search_key(search_url, config)
    bucket = get_seen_bucket(state, key)
    prune_seen(bucket, int(config.get("seen_ttl_days") or 60))

    first_scan = not bucket.get("seen")
    new_items = update_seen(bucket, listings)
    save_json_file(state_path, state)

    displayed = [] if hide_seen and first_scan else (new_items if hide_seen else listings)
    center = get_filter_center(config)
    new_ids = {item.id for item in new_items}
    return {
        "ok": True,
        "search_url": search_url,
        "api_url": api_url,
        "total_matches": len(listings),
        "new_count": len(new_items),
        "first_scan": first_scan,
        "hide_seen": hide_seen,
        "displayed_count": len(displayed),
        "center": center,
        "listings": listings_to_payloads(displayed, new_ids, center=center, category=category),
    }


def listings_to_payloads(
    listings: list[Listing],
    new_ids: set[str],
    center: dict[str, float] | None,
    category: str,
) -> list[dict[str, Any]]:
    if not listings:
        return []
    workers = min(8, max(1, len(listings)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(
            lambda item: listing_to_payload(item, item.id in new_ids, center=center, category=category),
            listings,
        ))


def get_filter_center(config: dict[str, Any]) -> dict[str, float] | None:
    filters = config.get("local_filters") or {}
    lat = parse_number(filters.get("center_lat"))
    lon = parse_number(filters.get("center_lon"))
    radius = parse_number(filters.get("radius_km"))
    if lat is None or lon is None:
        return None
    payload = {"lat": lat, "lon": lon}
    if radius is not None:
        payload["radius_km"] = radius
    return payload


def listing_to_payload(
    listing: Listing,
    is_new: bool = False,
    center: dict[str, float] | None = None,
    category: str = "home",
) -> dict[str, Any]:
    distance = None
    if center and listing.lat is not None and listing.lon is not None:
        distance = round(haversine_km(center["lat"], center["lon"], listing.lat, listing.lon), 2)
    description_short = shorten(listing.description, 900)
    title_en = translate_to_english_if_needed(listing.title, max_length=240)
    description_en = translate_to_english_if_needed(description_short, max_length=900)
    translated_cost_items = []
    for item in listing.cost_items:
        translated = dict(item)
        translated["text_en"] = translate_to_english_if_needed(str(item.get("text") or ""), max_length=240)
        translated_cost_items.append(translated)
    return {
        "id": listing.id,
        "category": category,
        "title": listing.title,
        "title_en": title_en,
        "url": listing.url,
        "price_value": listing.price_value,
        "price_label": listing.price_label,
        "rent_value": listing.rent_value,
        "area_m2": listing.area_m2,
        "rooms_label": listing.rooms_label,
        "furniture_key": listing.furniture_key,
        "furniture_label": listing.furniture_label,
        "location": listing.location,
        "district": listing.district,
        "lat": listing.lat,
        "lon": listing.lon,
        "map_radius": listing.map_radius,
        "distance_km": distance,
        "created_time": listing.created_time,
        "refresh_time": listing.refresh_time,
        "description": listing.description,
        "description_short": description_short,
        "description_en": description_en,
        "photos": listing.photos,
        "cost_items": translated_cost_items,
        "details": listing.details,
        "total_known_cost": listing.total_known_cost,
        "has_photo": listing.has_photo,
        "is_new": is_new,
    }


def print_scan_summary(
    search_url: str,
    api_url: str,
    listings: list[Listing],
    new_items: list[Listing],
    first_scan: bool,
    dry_run: bool = False,
    show_all_when_no_new: bool = True,
) -> None:
    print("")
    print(f"Search URL: {search_url}")
    print(f"API URL: {api_url}")
    print(f"Found after local filters: {len(listings)}")
    if dry_run:
        print("Dry run: seen-state was not written.")
    elif first_scan and not new_items:
        print("First scan: current listings were saved as seen; no notification sent.")
    else:
        print(f"New listings: {len(new_items)}")

    display_items = new_items or (listings if show_all_when_no_new else [])
    if not display_items:
        return

    print("")
    for index, listing in enumerate(display_items[: int(os.getenv("OLX_DISPLAY_LIMIT", "20"))], start=1):
        print(format_listing_line(index, listing))
        print(f"   {listing.url}")
    if len(display_items) > 20:
        print(f"... {len(display_items) - 20} more not shown. Set OLX_DISPLAY_LIMIT to change this.")


def format_listing_line(index: int, listing: Listing) -> str:
    bits = [
        f"{index:02d}. {listing.title}",
        listing.price_label or format_money(listing.price_value),
        f"{listing.area_m2:g} m2" if listing.area_m2 is not None else "",
        listing.rooms_label,
        listing.location,
    ]
    if listing.rent_value is not None:
        bits.append(f"rent +{listing.rent_value:g} PLN")
    if listing.total_known_cost is not None and listing.rent_value is not None:
        bits.append(f"known total {listing.total_known_cost:g} PLN")
    return " | ".join(bit for bit in bits if bit)


def format_money(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g} PLN"


def notify(listings: list[Listing], config: dict[str, Any]) -> None:
    notification_config = config.get("notifications") or {}
    max_items = int(notification_config.get("max_items_per_scan") or 10)
    selected = listings[:max_items]

    if notification_config.get("console", True):
        print("")
        print(f"NOTIFICATION: {len(listings)} new listing(s).")

    if notification_config.get("beep", True):
        beep()

    telegram_config = notification_config.get("telegram") or {}
    if telegram_config.get("enabled"):
        send_telegram(selected, len(listings), telegram_config)

    ntfy_config = notification_config.get("ntfy") or {}
    if ntfy_config.get("enabled"):
        send_ntfy(selected, len(listings), ntfy_config)

    command = notification_config.get("command")
    if command:
        run_notification_command(selected, command)


def beep() -> None:
    try:
        if os.name == "nt":
            import winsound

            winsound.Beep(900, 250)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def send_telegram(listings: list[Listing], total_count: int, config: dict[str, Any]) -> None:
    message = build_notification_message(listings, total_count)
    try:
        send_telegram_message(message, config)
    except RuntimeError as exc:
        print(f"Telegram notification skipped: {exc}")


def send_telegram_message(message: str, config: dict[str, Any]) -> None:
    token = str(config.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(config.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram bot_token/chat_id is missing.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()


def send_telegram_scan_payloads(
    listings: list[dict[str, Any]],
    total_count: int,
    config: dict[str, Any],
) -> int:
    notification_config = config.get("notifications") or {}
    telegram_config = notification_config.get("telegram") or {}
    max_items = int(notification_config.get("max_items_per_scan") or 10)
    selected = listings[:max_items]
    message = build_scan_payload_message(selected, total_count, len(listings))
    send_telegram_message(message, telegram_config)
    return len(selected)


def build_scan_payload_message(
    listings: list[dict[str, Any]],
    total_count: int,
    displayed_count: int,
) -> str:
    lines = [f"home-scanner: {displayed_count} displayed listing(s), {total_count} total match(es)"]
    for index, listing in enumerate(listings, start=1):
        lines.append("")
        lines.append(format_payload_listing_line(index, listing))
        url = str(listing.get("url") or "").strip()
        if url:
            lines.append(url)
    if len(listings) < displayed_count:
        lines.append("")
        lines.append(f"...and {displayed_count - len(listings)} more displayed listing(s).")
    return "\n".join(lines)


def format_payload_listing_line(index: int, listing: dict[str, Any]) -> str:
    title = str(listing.get("title_en") or listing.get("title") or "").strip()
    bits = [
        f"{index:02d}. {title}",
        str(listing.get("price_label") or "").strip(),
        f"{listing.get('area_m2'):g} m2" if isinstance(listing.get("area_m2"), int | float) else "",
        str(listing.get("rooms_label") or "").strip(),
        str(listing.get("location") or "").strip(),
    ]
    distance = listing.get("distance_km")
    if isinstance(distance, int | float):
        bits.append(f"{distance:g} km")
    total = listing.get("total_known_cost")
    if isinstance(total, int | float):
        bits.append(f"known total {total:g} PLN")
    return " | ".join(bit for bit in bits if bit)


def send_ntfy(listings: list[Listing], total_count: int, config: dict[str, Any]) -> None:
    server = str(config.get("server") or "https://ntfy.sh").rstrip("/")
    topic = str(config.get("topic") or os.getenv("NTFY_TOPIC") or "").strip()
    if not topic:
        print("ntfy notification skipped: topic is missing.")
        return
    message = build_notification_message(listings, total_count)
    response = requests.post(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": f"OLX: {total_count} new rental listing(s)"},
        timeout=20,
    )
    response.raise_for_status()


def build_notification_message(listings: list[Listing], total_count: int) -> str:
    lines = [f"OLX: {total_count} new rental listing(s)"]
    for index, listing in enumerate(listings, start=1):
        lines.append("")
        lines.append(format_listing_line(index, listing))
        lines.append(listing.url)
    if len(listings) < total_count:
        lines.append("")
        lines.append(f"...and {total_count - len(listings)} more.")
    return "\n".join(lines)


def run_notification_command(listings: list[Listing], command: Any) -> None:
    if not isinstance(command, list) or not command:
        print("Notification command must be a non-empty JSON array.")
        return
    for listing in listings:
        env = os.environ.copy()
        env.update(
            {
                "OLX_LISTING_ID": listing.id,
                "OLX_LISTING_TITLE": listing.title,
                "OLX_LISTING_URL": listing.url,
                "OLX_LISTING_PRICE": listing.price_label,
                "OLX_LISTING_LOCATION": listing.location,
            }
        )
        subprocess.run([str(part) for part in command], env=env, timeout=30, check=False)


def watch(config: dict[str, Any], state_path: Path) -> None:
    interval_minutes = float(config.get("scan_interval_minutes") or 10)
    interval_seconds = max(60.0, interval_minutes * 60.0)
    commands: queue.Queue[str] = queue.Queue()
    start_input_thread(commands)

    print(f"Watching OLX every {interval_seconds / 60:g} minute(s).")
    print("Type 'scan' and press Enter for an immediate scan, or 'q' to quit.")

    next_scan = 0.0
    while True:
        now = time.monotonic()
        should_scan = now >= next_scan

        try:
            command = commands.get_nowait()
        except queue.Empty:
            command = ""

        if command in {"q", "quit", "exit"}:
            print("Exiting.")
            return
        if command == "scan":
            should_scan = True

        if should_scan:
            try:
                scan_once(config, state_path, show_all_when_no_new=False)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"Scan failed: {exc}", file=sys.stderr)
            next_scan = time.monotonic() + interval_seconds

        time.sleep(0.5)


def start_input_thread(commands: queue.Queue[str]) -> None:
    def read_input() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            commands.put(line.strip().casefold())

    thread = threading.Thread(target=read_input, name="stdin-listener", daemon=True)
    thread.start()


def serve_web(config_path: Path, state_path: Path, host: str, port: int) -> None:
    handler = make_web_handler(config_path, state_path)
    server = ThreadingHTTPServer((host, port), handler)
    url_host = "localhost" if host in {"127.0.0.1", "0.0.0.0", ""} else host
    print(f"{APP_NAME} UI: http://{url_host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        server.server_close()


def make_web_handler(config_path: Path, state_path: Path) -> type[BaseHTTPRequestHandler]:
    class WebHandler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/1.0"

        def log_message(self, format_text: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} - {format_text % args}")

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self.write_html(INDEX_HTML)
                    return
                if parsed.path == "/api/config":
                    config = load_json_file(config_path, default=load_json_file(Path(__file__).with_name("config.example.json")))
                    self.write_json({
                        "ok": True,
                        "config": config_to_ui_payload(config),
                        "cities": CITY_OPTIONS,
                        "categories": [{"key": key, **value} for key, value in CATEGORY_OPTIONS.items()],
                    })
                    return
                if parsed.path == "/api/districts":
                    params = parse_qs(parsed.query)
                    city_slug = (params.get("city_slug") or ["krakow"])[0]
                    category = normalize_category((params.get("category") or ["home"])[0])
                    self.write_json({"ok": True, "districts": fetch_districts(city_slug, category)})
                    return
                if parsed.path == "/api/geocode":
                    params = parse_qs(parsed.query)
                    address = (params.get("address") or [""])[0]
                    city_slug = (params.get("city_slug") or ["krakow"])[0]
                    self.write_json({"ok": True, "result": geocode_address(address, city_slug)})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.write_json({"ok": False, "error": str(exc)}, status=500)

        def do_POST(self) -> None:
            try:
                payload = self.read_json()
                base_config = load_json_file(
                    config_path,
                    default=load_json_file(Path(__file__).with_name("config.example.json")),
                )

                if self.path == "/api/scan":
                    config = ui_payload_to_config(payload, base_config)
                    if payload.get("save_config"):
                        save_json_file(config_path, config)
                    result = scan_for_ui(config, state_path, hide_seen=bool(payload.get("hide_seen")))
                    self.write_json(result)
                    return

                if self.path == "/api/config":
                    config = ui_payload_to_config(payload, base_config)
                    save_json_file(config_path, config)
                    self.write_json({"ok": True, "config": config_to_ui_payload(config)})
                    return

                if self.path == "/api/telegram/send":
                    listings = payload.get("listings") or []
                    if not isinstance(listings, list):
                        raise ValueError("listings must be an array.")
                    total_count = int(coerce_number(payload.get("total_matches"), len(listings)) or len(listings))
                    sent_count = send_telegram_scan_payloads(
                        [item for item in listings if isinstance(item, dict)],
                        total_count,
                        base_config,
                    )
                    self.write_json({"ok": True, "sent_count": sent_count})
                    return

                if self.path == "/api/seen/reset":
                    config = ui_payload_to_config(payload, base_config)
                    removed = reset_seen_for_config(config, state_path)
                    self.write_json({"ok": True, "removed": removed})
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.write_json({"ok": False, "error": str(exc)}, status=500)

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("JSON payload must be an object.")
            return data

        def write_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def write_html(self, html_text: str) -> None:
            body = html_text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WebHandler


def ui_payload_to_config(payload: dict[str, Any], base_config: dict[str, Any]) -> dict[str, Any]:
    config = json.loads(json.dumps(base_config, ensure_ascii=False))
    config["search_url"] = ""
    config["max_pages"] = int(coerce_number(payload.get("max_pages"), 2) or 2)
    config["scan_interval_minutes"] = float(coerce_number(payload.get("scan_interval_minutes"), 10) or 10)
    config["ui_defaults"] = {"address": str(payload.get("address") or "").strip()}
    category = normalize_category(payload.get("category"))

    rooms = [room for room in (normalize_room(item) for item in as_list(payload.get("rooms"))) if room]
    furniture = [
        item
        for item in (normalize_furniture(value) for value in as_list(payload.get("furniture")))
        if item
    ]

    olx_filters = config.setdefault("olx_filters", {})
    olx_filters["category_path"] = category_path_for(category)
    olx_filters["city_slug"] = str(payload.get("city_slug") or "krakow").strip().strip("/") or "krakow"
    olx_filters["query"] = str(payload.get("query") or "").strip()
    default_sort = str(CATEGORY_OPTIONS[category]["default_sort"])
    sort_mode = str(payload.get("sort") or default_sort)
    if category != "home" and sort_mode.startswith("known_total:"):
        sort_mode = default_sort
    config["ui"] = {"sort_mode": sort_mode, "category": category}
    olx_filters["sort"] = "filter_float_price:asc" if sort_mode.startswith("known_total:") else sort_mode
    owner_type = str(payload.get("owner_type") or "all")
    olx_filters["owner_type"] = None if owner_type == "all" else owner_type
    olx_filters["price_from"] = coerce_number(payload.get("price_from"))
    olx_filters["price_to"] = coerce_number(payload.get("price_to"), 2000 if category == "home" else None)
    olx_filters["area_from"] = coerce_number(payload.get("area_from")) if category == "home" else None
    olx_filters["area_to"] = coerce_number(payload.get("area_to")) if category == "home" else None
    olx_filters["rooms"] = rooms if category == "home" else []
    olx_filters["furniture"] = furniture if category == "home" else []
    olx_filters["year_from"] = coerce_number(payload.get("year_from")) if category == "car" else None
    olx_filters["year_to"] = coerce_number(payload.get("year_to")) if category == "car" else None
    olx_filters["mileage_from"] = coerce_number(payload.get("mileage_from")) if category == "car" else None
    olx_filters["mileage_to"] = coerce_number(payload.get("mileage_to")) if category == "car" else None
    olx_filters["district_id"] = coerce_number(payload.get("district_id"))
    olx_filters["distance_km"] = coerce_number(payload.get("distance_km"))
    olx_filters["only_with_photo"] = bool(payload.get("only_with_photo"))

    local_filters = empty_local_filters()
    local_filters["category"] = category
    local_filters["min_price"] = olx_filters["price_from"]
    local_filters["max_price"] = olx_filters["price_to"]
    local_filters["min_area_m2"] = olx_filters["area_from"]
    local_filters["max_area_m2"] = olx_filters["area_to"]
    local_filters["rooms"] = rooms
    local_filters["furniture"] = furniture
    local_filters["require_photo"] = bool(payload.get("only_with_photo"))
    local_filters["districts_any"] = clean_string_list(payload.get("districts_any"))
    local_filters["keywords_any"] = clean_string_list(payload.get("keywords_any"))
    local_filters["keywords_all"] = clean_string_list(payload.get("keywords_all"))
    local_filters["exclude_keywords"] = clean_string_list(payload.get("exclude_keywords"))
    local_filters["max_total_known_cost"] = coerce_number(payload.get("max_total_known_cost"), 2500)
    local_filters["apply_total_limit"] = bool(payload.get("apply_total_limit"))
    local_filters["center_lat"] = coerce_number(payload.get("center_lat"))
    local_filters["center_lon"] = coerce_number(payload.get("center_lon"))
    local_filters["radius_km"] = coerce_number(payload.get("radius_km"), 5)
    local_filters["apply_radius_filter"] = bool(payload.get("apply_radius_filter"))
    config["local_filters"] = local_filters

    return config


def config_to_ui_payload(config: dict[str, Any]) -> dict[str, Any]:
    olx_filters = config.get("olx_filters") or {}
    local_filters = config.get("local_filters") or {}
    ui_config = config.get("ui") or {}
    category = normalize_category(ui_config.get("category") or olx_filters.get("category_path"))
    return {
        "category": category,
        "city_slug": olx_filters.get("city_slug") or "krakow",
        "query": olx_filters.get("query") or "",
        "sort": ui_config.get("sort_mode") or olx_filters.get("sort") or CATEGORY_OPTIONS[category]["default_sort"],
        "owner_type": olx_filters.get("owner_type") or "all",
        "price_from": olx_filters.get("price_from"),
        "price_to": olx_filters.get("price_to") if olx_filters.get("price_to") is not None else (2000 if category == "home" else None),
        "area_from": olx_filters.get("area_from"),
        "area_to": olx_filters.get("area_to"),
        "year_from": olx_filters.get("year_from"),
        "year_to": olx_filters.get("year_to"),
        "mileage_from": olx_filters.get("mileage_from"),
        "mileage_to": olx_filters.get("mileage_to"),
        "rooms": olx_filters.get("rooms") or [],
        "furniture": olx_filters.get("furniture") or local_filters.get("furniture") or [],
        "district_id": olx_filters.get("district_id"),
        "distance_km": olx_filters.get("distance_km"),
        "only_with_photo": bool(olx_filters.get("only_with_photo")),
        "districts_any": local_filters.get("districts_any") or [],
        "keywords_any": local_filters.get("keywords_any") or [],
        "keywords_all": local_filters.get("keywords_all") or [],
        "exclude_keywords": local_filters.get("exclude_keywords") or [],
        "max_total_known_cost": local_filters.get("max_total_known_cost") if local_filters.get("max_total_known_cost") is not None else 2500,
        "apply_total_limit": bool(local_filters.get("apply_total_limit")),
        "center_lat": local_filters.get("center_lat"),
        "center_lon": local_filters.get("center_lon"),
        "radius_km": local_filters.get("radius_km") if local_filters.get("radius_km") is not None else 5,
        "apply_radius_filter": bool(local_filters.get("apply_radius_filter")),
        "address": (config.get("ui_defaults") or {}).get("address") or "",
        "max_pages": config.get("max_pages") or 2,
        "scan_interval_minutes": config.get("scan_interval_minutes") or 10,
    }


def fetch_districts(city_slug: str, category: str = "home") -> list[dict[str, Any]]:
    city_slug = str(city_slug or "krakow").strip().strip("/") or "krakow"
    search_url = f"{OLX_ROOT}/{category_path_for(normalize_category(category))}/{city_slug}/"
    session = make_session({})
    response = session.get(search_url, timeout=30)
    response.raise_for_status()
    state = extract_prerendered_state(response.text)
    listing = (state.get("listing") or {}).get("listing") or {}
    facets = ((listing.get("metaData") or {}).get("facets") or {}).get("district") or []
    districts = []
    for item in facets:
        district_id = item.get("id")
        label = item.get("label")
        if district_id is None or not label:
            continue
        districts.append({
            "id": district_id,
            "label": str(label),
            "count": item.get("count") or 0,
        })
    return sorted(districts, key=lambda item: item["label"].casefold())


def geocode_address(address: str, city_slug: str) -> dict[str, Any] | None:
    address = str(address or "").strip()
    if not address:
        return None
    city = CITY_BY_SLUG.get(str(city_slug or "krakow"), CITY_BY_SLUG["krakow"])
    query = address
    if city["name"].casefold() not in address.casefold():
        query = f"{address}, {city['name']}, Polska"

    response = requests.get(
        "https://photon.komoot.io/api/",
        params={"q": query, "limit": 1},
        headers={"User-Agent": f"{APP_NAME}/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    features = response.json().get("features") or []
    if not features:
        return None

    feature = features[0]
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    props = feature.get("properties") or {}
    if len(coords) < 2:
        return None
    return {
        "lat": float(coords[1]),
        "lon": float(coords[0]),
        "label": ", ".join(
            part
            for part in [
                props.get("name"),
                props.get("district"),
                props.get("city"),
                props.get("country"),
            ]
            if part
        ),
    }


def reset_seen_for_config(config: dict[str, Any], state_path: Path) -> bool:
    state = load_json_file(state_path, default={"searches": {}})
    search_url = build_search_url(config)
    key = search_key(search_url, config)
    searches = state.setdefault("searches", {})
    removed = key in searches
    searches.pop(key, None)
    save_json_file(state_path, state)
    return removed


def coerce_number(value: Any, default: float | int | None = None) -> float | int | None:
    if value is None or value == "":
        return default
    number = parse_number(value)
    if number is None:
        return default
    if float(number).is_integer():
        return int(number)
    return number


def clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = as_list(value)
    return [str(item).strip() for item in items if str(item).strip()]


def translate_to_english_if_needed(text: str, max_length: int = 900) -> str:
    text = shorten(str(text or ""), max_length)
    if not text or looks_english(text):
        return text

    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with _TRANSLATION_CACHE_LOCK:
        cache = get_translation_cache()
        cached = str(cache.get(key) or "")
        if cached:
            if not is_translation_limit_error(cached):
                return cached
            cache.pop(key, None)

    translated_parts = []
    for chunk in translation_chunks(text, max_chars=1200):
        translated_parts.append(translate_chunk_to_english(chunk))
    translated = "\n".join(part for part in translated_parts if part).strip()
    if translated and not is_translation_limit_error(translated):
        with _TRANSLATION_CACHE_LOCK:
            cache = get_translation_cache()
            cache[key] = translated
            save_translation_cache(cache)
        return translated
    return text


def is_translation_limit_error(text: str) -> bool:
    normalized = str(text or "").upper()
    return "QUERY LENGTH LIMIT EXCEEDED" in normalized or "MAX ALLOWED QUERY" in normalized


def translation_chunks(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for start in range(0, len(sentence), max_chars):
                chunks.append(sentence[start : start + max_chars].strip())
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = sentence
    if current:
        chunks.append(current.strip())
    return chunks


def translate_chunk_to_english(text: str) -> str:
    try:
        response = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text[:1200]},
            headers={"User-Agent": f"{APP_NAME}/1.0"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        segments = payload[0] if isinstance(payload, list) and payload else []
        translated = "".join(str(segment[0] or "") for segment in segments if isinstance(segment, list) and segment).strip()
        if translated and not is_translation_limit_error(translated):
            return translated
    except Exception:
        pass
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:400], "langpair": "pl|en"},
            headers={"User-Agent": f"{APP_NAME}/1.0"},
            timeout=6,
        )
        response.raise_for_status()
        payload = response.json()
        translated = str((payload.get("responseData") or {}).get("translatedText") or "").strip()
        if translated and not is_translation_limit_error(translated):
            return translated
    except Exception:
        pass
    return text


def looks_english(text: str) -> bool:
    normalized = f" {normalize_text(text)} "
    polish_markers = (
        "ą", "ć", "ę", "ł", "ń", "ó", "ś", "ź", "ż",
        " mieszkanie ", " wynajem ", " wynajm", " czynsz ", " pokoje ",
        " pokój ", " pokoj ", " kaucj", " opłat", " oplat", " bezpośrednio",
        " bezposrednio", " dostęp", " dostep", " piętro", " pietro",
    )
    if any(marker in normalized for marker in polish_markers):
        return False
    english_markers = (
        " apartment ", " rent ", " rental ", " available ", " room ", " rooms ",
        " included ", " bills ", " utilities ", " deposit ", " near ", " with ",
    )
    return sum(1 for marker in english_markers if marker in normalized) >= 2


def get_translation_cache() -> dict[str, str]:
    global _TRANSLATION_CACHE
    if _TRANSLATION_CACHE is None:
        _TRANSLATION_CACHE = load_json_file(DEFAULT_TRANSLATION_CACHE_PATH, default={})
    return _TRANSLATION_CACHE


def save_translation_cache(cache: dict[str, str]) -> None:
    try:
        save_json_file(DEFAULT_TRANSLATION_CACHE_PATH, cache)
    except Exception:
        pass


INDEX_HTML = r"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>home-scanner</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {
      --bg: #11130f;
      --panel: #191d17;
      --panel-2: #20251d;
      --field: #0d0f0c;
      --line: #343b31;
      --ink: #f1efe4;
      --muted: #a2aa98;
      --soft: #c9d0bc;
      --accent: #49c5a2;
      --accent-2: #e0a33a;
      --danger: #ff6f61;
      --shadow: rgba(0, 0, 0, 0.34);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 18% 12%, rgba(73, 197, 162, 0.12), transparent 28%),
        linear-gradient(90deg, rgba(241,239,228,0.035) 1px, transparent 1px),
        linear-gradient(180deg, rgba(241,239,228,0.03) 1px, transparent 1px),
        var(--bg);
      background-size: auto, 34px 34px, 34px 34px, auto;
      font-family: Bahnschrift, "Segoe UI Variable", "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: minmax(340px, 450px) 1fr; }
    aside {
      border-right: 1px solid var(--line);
      background: rgba(17, 19, 15, 0.9);
      padding: 22px;
      overflow: auto;
      max-height: 100vh;
    }
    main { padding: 22px 26px 32px; overflow: auto; max-height: 100vh; }
    .brand { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 25px; line-height: 1.08; font-weight: 820; }
    .stamp {
      border: 1px solid var(--accent);
      color: var(--accent);
      padding: 5px 8px;
      font-size: 11px;
      text-transform: uppercase;
      white-space: nowrap;
      background: rgba(73, 197, 162, 0.08);
    }
    .section { border-top: 1px solid var(--line); padding-top: 14px; margin-top: 14px; }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      color: var(--soft);
      margin-bottom: 10px;
    }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .field { display: grid; gap: 5px; margin-bottom: 10px; }
    label { font-size: 12px; color: var(--soft); font-weight: 650; }
    input, select {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      background: var(--field);
      color: var(--ink);
      padding: 0 10px;
      border-radius: 6px;
      outline: none;
    }
    input:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(73, 197, 162, 0.14); }
    select option { background: var(--field); color: var(--ink); }
    .checks { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .checks.two { grid-template-columns: 1fr 1fr; }
    .category-choice { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .category-choice label {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      cursor: pointer;
      color: var(--ink);
      font-weight: 820;
    }
    .category-choice input { width: 16px; height: 16px; accent-color: var(--accent); }
    body[data-category="car"] .home-only,
    body[data-category="home"] .car-only { display: none !important; }
    .checks label, .switch {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      min-height: 38px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      cursor: pointer;
      color: var(--ink);
    }
    .checks input, .switch input { width: 16px; height: 16px; accent-color: var(--accent); }
    .actions { padding-top: 20px; display: grid; gap: 9px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
    button {
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--ink);
      min-height: 40px;
      border-radius: 6px;
      padding: 0 12px;
      cursor: pointer;
      font-weight: 760;
    }
    button:hover { box-shadow: 0 10px 24px var(--shadow); border-color: var(--accent); }
    button:disabled { opacity: 0.55; cursor: wait; transform: none; }
    button.accent { border-color: #277d68; background: var(--accent); color: #07100c; }
    button.warning { border-color: #8a6427; background: rgba(224, 163, 58, 0.16); color: #f5d394; }
    button.active { border-color: var(--accent-2); color: #f5d394; background: rgba(224, 163, 58, 0.13); }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding-bottom: 16px;
      margin-bottom: 16px;
    }
    .status { min-height: 42px; display: flex; align-items: center; color: var(--muted); font-size: 14px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; margin-bottom: 16px; }
    .metric { border: 1px solid var(--line); background: rgba(25, 29, 23, 0.9); border-radius: 8px; padding: 12px; min-height: 74px; }
    .metric b { display: block; font-size: 26px; line-height: 1; }
    .metric span { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 750; }
    .map-shell { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; height: 360px; margin-bottom: 16px; background: #0b0d0a; }
    #map { width: 100%; height: 100%; }
    .leaflet-container { background: #0b0d0a; color: #11130f; font-family: inherit; }
    .results { display: grid; gap: 10px; }
    .listing {
      border: 1px solid var(--line);
      background: rgba(25, 29, 23, 0.94);
      border-radius: 8px;
      padding: 14px;
      display: grid;
      gap: 10px;
      box-shadow: 0 10px 22px rgba(0, 0, 0, 0.18);
      cursor: default;
    }
    .listing.active { border-color: var(--accent-2); box-shadow: inset 4px 0 0 var(--accent-2), 0 10px 22px rgba(224, 163, 58, 0.14); }
    .listing.new { border-color: var(--accent); box-shadow: inset 4px 0 0 var(--accent), 0 10px 22px rgba(73, 197, 162, 0.13); }
    .listing.favorite { border-color: rgba(224, 163, 58, 0.62); }
    .listing h2 { margin: 0; font-size: 17px; line-height: 1.25; }
    .title-line { display: flex; align-items: flex-start; gap: 10px; }
    .title-line h2 { flex: 1; }
    .favorite-toggle {
      flex: 0 0 32px;
      width: 32px;
      min-height: 32px;
      padding: 0;
      color: #f5d394;
      font-size: 19px;
      line-height: 1;
    }
    .favorite-toggle.active { border-color: var(--accent-2); background: rgba(224, 163, 58, 0.18); color: #ffd56d; }
    .num-badge {
      flex: 0 0 auto;
      min-width: 28px;
      height: 28px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background: var(--accent-2);
      color: #11130f;
      font-weight: 900;
      font-size: 13px;
    }
    .map-pin {
      width: 30px;
      height: 30px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background: var(--accent-2);
      color: #11130f;
      border: 2px solid #f1efe4;
      box-shadow: 0 8px 18px rgba(0, 0, 0, 0.34);
      font: 900 13px Bahnschrift, "Segoe UI", sans-serif;
    }
    .map-pin.new { background: var(--accent); }
    .map-pin.approx { background: #6f7780; color: #f7f5ea; }
    .photo-strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .photo-strip { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); }
    .photo-thumb {
      border: 0;
      padding: 0;
      background: transparent;
      cursor: zoom-in;
      border-radius: 6px;
      overflow: hidden;
    }
    .photo-thumb img {
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #0b0d0a;
      display: block;
    }
    .photo-viewer {
      position: fixed;
      inset: 0;
      background: rgba(8, 10, 8, 0.84);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 20px;
    }
    .photo-viewer.open { display: flex; }
    .photo-viewer-panel {
      position: relative;
      max-width: min(96vw, 1280px);
      max-height: 92vh;
      display: grid;
      gap: 8px;
      justify-items: end;
    }
    .photo-viewer-stage {
      position: relative;
      display: grid;
      align-items: center;
      justify-items: center;
    }
    .photo-viewer img {
      max-width: 96vw;
      max-height: 82vh;
      object-fit: contain;
      border-radius: 10px;
      box-shadow: 0 18px 56px rgba(0, 0, 0, 0.5);
      background: #0b0d0a;
    }
    .photo-nav {
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      border: 1px solid var(--line);
      background: rgba(25, 29, 23, 0.9);
      color: var(--ink);
      width: 46px;
      height: 46px;
      border-radius: 999px;
      cursor: pointer;
      display: grid;
      place-items: center;
      font-size: 24px;
      line-height: 1;
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.35);
    }
    .photo-nav:hover { border-color: var(--accent-2); }
    .photo-nav.prev { left: -12px; }
    .photo-nav.next { right: -12px; }
    .photo-counter {
      justify-self: center;
      color: var(--soft);
      font-size: 12px;
      font-weight: 750;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .photo-viewer-close {
      border: 1px solid var(--line);
      background: rgba(25, 29, 23, 0.95);
      color: var(--ink);
      min-width: 38px;
      min-height: 38px;
      border-radius: 999px;
      cursor: pointer;
      font-size: 22px;
      line-height: 1;
    }
    .description { color: var(--soft); font-size: 13px; line-height: 1.45; white-space: pre-wrap; }
    .cost-box { border: 1px solid rgba(224, 163, 58, 0.38); background: rgba(224, 163, 58, 0.08); border-radius: 6px; padding: 10px; display: grid; gap: 6px; }
    .cost-title { color: #f5d394; font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .cost-item { color: var(--soft); font-size: 12px; line-height: 1.35; }
    .facts { display: flex; flex-wrap: wrap; gap: 7px; }
    .fact { border: 1px solid var(--line); border-radius: 999px; padding: 5px 9px; font-size: 12px; background: #12150f; color: var(--soft); }
    .listing-actions { display: flex; justify-content: flex-end; }
    .listing-actions button { min-height: 34px; color: var(--accent); }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 28px;
      background: rgba(25, 29, 23, 0.64);
    }
    .error { color: var(--danger); font-weight: 750; }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.35; margin-top: -4px; }
    @media (max-width: 960px) {
      .shell { grid-template-columns: 1fr; }
      aside, main { max-height: none; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .toolbar, .metrics { grid-template-columns: 1fr; }
    }
    @media (max-width: 540px) {
      .grid, .row { grid-template-columns: 1fr; }
      .checks { grid-template-columns: 1fr 1fr; }
      aside, main { padding: 16px; }
      .map-shell { height: 300px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">
        <h1>home-scanner</h1>
        <div class="stamp">dark mode</div>
      </div>
      <form id="filters">
        <div class="section">
          <div class="section-title">Scan type</div>
          <div class="category-choice">
            <label><input type="radio" name="category" value="home"> Home</label>
            <label><input type="radio" name="category" value="car"> Car</label>
          </div>
        </div>

        <div class="section">
          <div class="section-title">Location</div>
          <div class="grid">
            <div class="field"><label for="city_slug">City</label><select id="city_slug"></select></div>
            <div class="field"><label for="district_id">District / area</label><select id="district_id"><option value="">All districts</option></select></div>
          </div>
          <div class="field"><label for="address">Around address</label><input id="address" placeholder="Rynek Główny 1"></div>
          <div class="grid">
            <div class="field"><label for="radius_km">Radius km</label><input id="radius_km" inputmode="decimal" placeholder="5"></div>
            <div class="field"><label>&nbsp;</label><button class="warning" type="button" id="geocode">Find address</button></div>
          </div>
          <label class="switch"><input type="checkbox" id="apply_radius_filter"> Apply address radius filter</label>
          <input type="hidden" id="center_lat"><input type="hidden" id="center_lon">
          <div class="hint" id="addressHint">The address is geocoded; the radius narrows results only when enabled.</div>
        </div>

        <div class="section">
          <div class="section-title">OLX filters</div>
          <div class="grid">
            <div class="field"><label for="owner_type">Seller type</label><select id="owner_type"><option value="all">All</option><option value="private">Private</option><option value="business">Business</option></select></div>
            <div class="field"><label for="sort">Sort</label><select id="sort"><option value="known_total:asc">Known total: cheapest first</option><option value="filter_float_price:asc">OLX price: cheapest first</option><option value="filter_float_price:desc">OLX price: most expensive first</option><option value="created_at:desc">Newest listings</option></select></div>
          </div>
          <div class="field"><label for="query">Search query</label><input id="query" placeholder="metro, balcony, garage"></div>
          <div class="grid home-only">
            <div class="field"><label for="price_from">Min price</label><input id="price_from" inputmode="numeric"></div>
            <div class="field"><label for="price_to">Max price</label><input id="price_to" inputmode="numeric"></div>
            <div class="field"><label for="area_from">Min m2</label><input id="area_from" inputmode="decimal"></div>
            <div class="field"><label for="area_to">Max m2</label><input id="area_to" inputmode="decimal"></div>
          </div>
          <div class="grid car-only">
            <div class="field"><label for="car_price_from">Min price</label><input id="car_price_from" inputmode="numeric"></div>
            <div class="field"><label for="car_price_to">Max price</label><input id="car_price_to" inputmode="numeric"></div>
            <div class="field"><label for="year_from">Min year</label><input id="year_from" inputmode="numeric"></div>
            <div class="field"><label for="year_to">Max year</label><input id="year_to" inputmode="numeric"></div>
            <div class="field"><label for="mileage_from">Min km</label><input id="mileage_from" inputmode="numeric"></div>
            <div class="field"><label for="mileage_to">Max km</label><input id="mileage_to" inputmode="numeric"></div>
          </div>
          <div class="field home-only"><label>Room count</label><div class="checks"><label><input type="checkbox" name="rooms" value="one"> 1</label><label><input type="checkbox" name="rooms" value="two"> 2</label><label><input type="checkbox" name="rooms" value="three"> 3</label><label><input type="checkbox" name="rooms" value="four"> 4+</label></div></div>
          <div class="field home-only"><label>Furnishing</label><div class="checks two"><label><input type="checkbox" name="furniture" value="yes"> Furnished</label><label><input type="checkbox" name="furniture" value="no"> Unfurnished</label></div></div>
          <label class="switch"><input type="checkbox" id="only_with_photo"> Only listings with photos</label>
        </div>

        <div class="section">
          <div class="section-title">Extra filters</div>
          <div class="field"><label for="keywords_any">Match any keyword</label><input id="keywords_any" placeholder="metro,balcony,tram"></div>
          <div class="field"><label for="keywords_all">Match all keywords</label><input id="keywords_all" placeholder="direct owner"></div>
          <div class="field"><label for="exclude_keywords">Exclude keywords</label><input id="exclude_keywords" placeholder="room share"></div>
          <div class="field home-only"><label for="max_total_known_cost">Known total limit</label><input id="max_total_known_cost" inputmode="numeric" placeholder="2500"></div>
          <label class="switch home-only"><input type="checkbox" id="apply_total_limit"> Apply known total limit</label>
        </div>

        <div class="section">
          <div class="section-title">Scan</div>
          <div class="grid">
            <div class="field"><label for="max_pages">Page count</label><input id="max_pages" inputmode="numeric" value="2"></div>
            <div class="field"><label for="scan_interval_minutes">Interval minutes</label><input id="scan_interval_minutes" inputmode="decimal" value="10"></div>
          </div>
          <label class="switch"><input type="checkbox" id="hide_seen"> Hide seen listings</label>
        </div>

        <div class="actions">
          <button class="accent" type="button" id="scan">Scan</button>
          <div class="row"><button type="button" id="watch">Start watching</button><button type="button" id="sendTelegram">Send scan to Telegram</button></div>
          <button type="button" id="save">Save filters</button>
          <button class="warning" type="button" id="resetSeen">Reset memory for this filter</button>
        </div>
      </form>
    </aside>
    <main>
      <div class="toolbar"><div class="status" id="status">Choose filters and press Scan.</div><button type="button" id="showAll">Show all</button><button type="button" id="showNew">New only</button><button type="button" id="showFavorites">Favorites</button></div>
      <div class="metrics"><div class="metric"><b id="metricTotal">0</b><span>matches</span></div><div class="metric"><b id="metricShown">0</b><span>shown</span></div><div class="metric"><b id="metricNew">0</b><span>new</span></div><div class="metric"><b id="metricMode">All</b><span>mode</span></div></div>
      <div class="map-shell"><div id="map"></div></div>
      <div class="results" id="results"><div class="empty">No scan has been run yet.</div></div>
    </main>
  </div>
  <div class="photo-viewer" id="photoViewer" aria-hidden="true">
    <div class="photo-viewer-panel" role="dialog" aria-modal="true" aria-label="Enlarged photo">
      <button type="button" class="photo-viewer-close" id="closePhotoViewer" aria-label="Close">×</button>
      <div class="photo-viewer-stage">
        <button type="button" class="photo-nav prev" id="photoPrev" aria-label="Previous photo">‹</button>
        <img id="photoViewerImage" alt="">
        <button type="button" class="photo-nav next" id="photoNext" aria-label="Next photo">›</button>
      </div>
      <div class="photo-counter" id="photoCounter"></div>
    </div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const $ = (id) => document.getElementById(id);
    const SORT_OPTIONS = {
      home: [
        ["known_total:asc", "Known total: cheapest first"],
        ["filter_float_price:asc", "OLX price: cheapest first"],
        ["filter_float_price:desc", "OLX price: most expensive first"],
        ["created_at:desc", "Newest listings"]
      ],
      car: [
        ["filter_float_price:asc", "OLX price: cheapest first"],
        ["filter_float_price:desc", "OLX price: most expensive first"],
        ["created_at:desc", "Newest listings"]
      ]
    };
    const FAVORITES_KEY = "home_scanner_favorites_v1";
    const LEGACY_FAVORITES_KEYS = ["olx_scanner_favorites_v1"];
    let watchTimer = null, map = null, markerLayer = null, radiusLayer = null, cities = [];
    let activeMarkerById = new Map();
    let lastData = null, lastListings = [], renderedListingsById = new Map(), favoriteMode = false;
    let photoGallery = [], photoGalleryIndex = 0;
    let favorites = loadFavorites();
    const csvToList = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
    const listToCsv = (value) => Array.isArray(value) ? value.join(",") : "";
    const valueOrNull = (id) => { const value = $(id).value.trim(); return value === "" ? null : value; };
    const selectedValues = (name) => [...document.querySelectorAll(`input[name='${name}']:checked`)].map((item) => item.value);
    const selectedCategory = () => document.querySelector("input[name='category']:checked")?.value || "home";

    function loadFavorites() {
      try {
        const currentRaw = localStorage.getItem(FAVORITES_KEY);
        if (currentRaw !== null) {
          const data = JSON.parse(currentRaw || "{}");
          return new Map(Object.entries(data || {}));
        }
        for (const legacyKey of LEGACY_FAVORITES_KEYS) {
          const legacyRaw = localStorage.getItem(legacyKey);
          if (legacyRaw === null) continue;
          const data = JSON.parse(legacyRaw || "{}");
          localStorage.setItem(FAVORITES_KEY, JSON.stringify(data || {}));
          return new Map(Object.entries(data || {}));
        }
        return new Map();
      } catch {
        return new Map();
      }
    }
    function saveFavorites() { localStorage.setItem(FAVORITES_KEY, JSON.stringify(Object.fromEntries(favorites))); }
    function updateFavoriteControl() {
      const button = $("showFavorites");
      if (!button) return;
      button.textContent = `Favorites (${favorites.size})`;
      button.classList.toggle("active", favoriteMode);
    }
    function setCategory(category, sortValue = null) {
      const normalized = category === "car" ? "car" : "home";
      document.body.dataset.category = normalized;
      document.querySelectorAll("input[name='category']").forEach((radio) => { radio.checked = radio.value === normalized; });
      const options = SORT_OPTIONS[normalized] || SORT_OPTIONS.home;
      $("sort").innerHTML = options.map(([value, label]) => `<option value="${escapeAttr(value)}">${escapeHtml(label)}</option>`).join("");
      const preferred = sortValue && options.some(([value]) => value === sortValue) ? sortValue : options[0][0];
      $("sort").value = preferred;
      if (normalized === "car") {
        $("car_price_from").value = $("price_from").value;
        $("car_price_to").value = $("price_to").value;
      } else {
        $("price_from").value = $("car_price_from").value;
        $("price_to").value = $("car_price_to").value || $("price_to").value;
      }
    }

    function collectPayload(extra = {}) {
      return {
        category: selectedCategory(),
        city_slug: $("city_slug").value || "krakow",
        district_id: valueOrNull("district_id"),
        address: valueOrNull("address"),
        center_lat: valueOrNull("center_lat"),
        center_lon: valueOrNull("center_lon"),
        radius_km: valueOrNull("radius_km"),
        query: valueOrNull("query") || "",
        owner_type: $("owner_type").value,
        sort: $("sort").value || (selectedCategory() === "home" ? "known_total:asc" : "filter_float_price:asc"),
        price_from: selectedCategory() === "car" ? valueOrNull("car_price_from") : valueOrNull("price_from"),
        price_to: selectedCategory() === "car" ? valueOrNull("car_price_to") : valueOrNull("price_to"),
        area_from: valueOrNull("area_from"),
        area_to: valueOrNull("area_to"),
        year_from: valueOrNull("year_from"),
        year_to: valueOrNull("year_to"),
        mileage_from: valueOrNull("mileage_from"),
        mileage_to: valueOrNull("mileage_to"),
        rooms: selectedValues("rooms"),
        furniture: selectedValues("furniture"),
        only_with_photo: $("only_with_photo").checked,
        districts_any: [],
        keywords_any: csvToList($("keywords_any").value),
        keywords_all: csvToList($("keywords_all").value),
        exclude_keywords: csvToList($("exclude_keywords").value),
        max_total_known_cost: valueOrNull("max_total_known_cost"),
        apply_total_limit: $("apply_total_limit").checked,
        max_pages: valueOrNull("max_pages") || 2,
        scan_interval_minutes: valueOrNull("scan_interval_minutes") || 10,
        apply_radius_filter: $("apply_radius_filter").checked,
        hide_seen: $("hide_seen").checked,
        ...extra
      };
    }

    function applyConfig(config) {
      setCategory(config.category || "home", config.sort || null);
      $("city_slug").value = config.city_slug || "krakow";
      $("address").value = config.address || "Opolska 110";
      $("query").value = config.query || "";
      $("owner_type").value = config.owner_type || "all";
      $("price_from").value = config.price_from ?? "";
      $("price_to").value = config.price_to ?? "";
      $("car_price_from").value = config.price_from ?? "";
      $("car_price_to").value = config.price_to ?? "";
      $("area_from").value = config.area_from ?? "";
      $("area_to").value = config.area_to ?? "";
      $("year_from").value = config.year_from ?? "";
      $("year_to").value = config.year_to ?? "";
      $("mileage_from").value = config.mileage_from ?? "";
      $("mileage_to").value = config.mileage_to ?? "";
      $("only_with_photo").checked = Boolean(config.only_with_photo);
      $("keywords_any").value = listToCsv(config.keywords_any);
      $("keywords_all").value = listToCsv(config.keywords_all);
      $("exclude_keywords").value = listToCsv(config.exclude_keywords);
      $("max_total_known_cost").value = config.max_total_known_cost ?? 2500;
      $("apply_total_limit").checked = Boolean(config.apply_total_limit);
      $("center_lat").value = config.center_lat ?? "";
      $("center_lon").value = config.center_lon ?? "";
      $("radius_km").value = config.radius_km ?? 5;
      $("apply_radius_filter").checked = Boolean(config.apply_radius_filter);
      $("max_pages").value = config.max_pages || 2;
      $("scan_interval_minutes").value = config.scan_interval_minutes || 10;
      document.querySelectorAll("input[name='rooms']").forEach((box) => { box.checked = (config.rooms || []).includes(box.value); });
      document.querySelectorAll("input[name='furniture']").forEach((box) => { box.checked = (config.furniture || []).includes(box.value); });
    }

    async function api(path, payload = null) {
      const options = payload ? { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) } : {};
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }
    function setBusy(isBusy) { document.querySelectorAll("button").forEach((button) => button.disabled = isBusy); }
    function populateCities(items) { cities = items || []; $("city_slug").innerHTML = cities.map((city) => `<option value="${escapeAttr(city.slug)}">${escapeHtml(city.name)}</option>`).join(""); }
    async function loadDistricts(selectedId = "") {
      const city = $("city_slug").value || "krakow";
      $("district_id").innerHTML = `<option value="">Loading...</option>`;
      try {
        const data = await api(`/api/districts?city_slug=${encodeURIComponent(city)}&category=${encodeURIComponent(selectedCategory())}`);
        $("district_id").innerHTML = [`<option value="">All districts</option>`].concat((data.districts || []).map((district) => `<option value="${escapeAttr(district.id)}">${escapeHtml(district.label)} (${district.count})</option>`)).join("");
        $("district_id").value = selectedId || "";
      } catch { $("district_id").innerHTML = `<option value="">Districts could not be loaded</option>`; }
    }
    async function geocodeAddress() {
      const address = valueOrNull("address");
      if (!address) {
        $("addressHint").textContent = "Enter an address before applying the radius filter.";
        $("center_lat").value = ""; $("center_lon").value = ""; updateMap([], null); return;
      }
      setBusy(true); $("addressHint").textContent = "Searching address...";
      try {
        const data = await api(`/api/geocode?city_slug=${encodeURIComponent($("city_slug").value || "krakow")}&address=${encodeURIComponent(address)}`);
        if (!data.result) { $("addressHint").textContent = "Address not found."; return; }
        $("center_lat").value = data.result.lat; $("center_lon").value = data.result.lon;
        $("addressHint").textContent = `Center: ${data.result.label || `${data.result.lat}, ${data.result.lon}`}`;
        updateMap([], {lat: data.result.lat, lon: data.result.lon, radius_km: Number(valueOrNull("radius_km") || 0)});
      } catch (error) { $("addressHint").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`; }
      finally { setBusy(false); }
    }
    async function scan(extra = {}) {
      favoriteMode = false;
      setBusy(true); $("status").textContent = "Scanning OLX...";
      try { const data = await api("/api/scan", collectPayload(extra)); render(data); return data; }
      catch (error) { $("status").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`; }
      finally { setBusy(false); }
    }
    async function sendCurrentScanToTelegram() {
      if (!lastData || !Array.isArray(lastListings)) {
        $("status").textContent = "Run a scan before sending results to Telegram.";
        return;
      }
      setBusy(true); $("status").textContent = "Sending scan results to Telegram...";
      try {
        const data = await api("/api/telegram/send", {
          listings: lastListings,
          total_matches: lastData.total_matches ?? lastListings.length
        });
        $("status").textContent = `Sent ${data.sent_count} listing(s) to Telegram.`;
      } catch (error) {
        $("status").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
      } finally {
        setBusy(false);
      }
    }
    function render(data) {
      lastData = data;
      lastListings = (data.listings || []).map((item, index) => ({...item, display_index: index + 1}));
      renderCurrentListings();
    }
    function favoriteListingsForDisplay() {
      const currentIds = new Set(lastListings.map((item) => String(item.id)));
      const merged = lastListings.filter((item) => favorites.has(String(item.id))).map((item) => ({...item, is_favorite: true}));
      favorites.forEach((item, id) => {
        if (!currentIds.has(String(id))) merged.push({...item, is_favorite: true, is_saved_favorite: true});
      });
      return merged.map((item, index) => ({...item, display_index: index + 1}));
    }
    function renderCurrentListings() {
      const data = lastData || {total_matches: 0, displayed_count: 0, new_count: 0, hide_seen: false, listings: [], center: null};
      const numberedListings = favoriteMode ? favoriteListingsForDisplay() : lastListings.map((item, index) => ({...item, display_index: index + 1}));
      renderedListingsById = new Map(numberedListings.map((item) => [String(item.id), item]));
      $("metricTotal").textContent = data.total_matches ?? 0;
      $("metricShown").textContent = numberedListings.length;
      $("metricNew").textContent = data.new_count ?? 0;
      $("metricMode").textContent = favoriteMode ? "Favorite" : (data.hide_seen ? "New" : "All");
      if (favoriteMode) $("status").textContent = `${numberedListings.length} favorites shown.`;
      else $("status").textContent = data.first_scan && data.hide_seen ? "First new-listing scan: current listings were saved as seen, so old listings are hidden." : `${data.displayed_count} listings shown. Total matches: ${data.total_matches}.`;
      updateFavoriteControl();
      updateMap(numberedListings, data.center);
      const results = $("results");
      if (numberedListings.length === 0) { results.innerHTML = `<div class="empty">${favoriteMode ? "No favorite listings." : (data.hide_seen ? "No new listings." : "No listings matched these filters.")}</div>`; return; }
      results.innerHTML = numberedListings.map(renderListing).join("");
      document.querySelectorAll(".photo-thumb").forEach((button) => button.addEventListener("click", (event) => {
        event.stopPropagation();
        openPhotoViewer(button.dataset.id || "", Number(button.dataset.index || "1") - 1);
      }));
      document.querySelectorAll(".favorite-toggle").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); toggleFavorite(button.dataset.id); }));
      document.querySelectorAll(".open-link").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); window.open(button.dataset.url, "_blank", "noopener"); }));
      document.querySelectorAll(".listing").forEach((card) => card.addEventListener("click", () => focusListing(card.dataset.id)));
    }
    function toggleFavorite(id) {
      const key = String(id);
      if (favorites.has(key)) favorites.delete(key);
      else {
        const item = renderedListingsById.get(key) || lastListings.find((entry) => String(entry.id) === key);
        if (item) favorites.set(key, {...item, is_favorite: true, saved_at: new Date().toISOString()});
      }
      saveFavorites();
      renderCurrentListings();
    }
    function openPhotoViewer(listingId, startIndex = 0) {
      const item = renderedListingsById.get(String(listingId)) || lastListings.find((entry) => String(entry.id) === String(listingId));
      const photos = (item && item.photos) ? item.photos.filter(Boolean) : [];
      if (!photos.length) return;
      photoGallery = photos;
      photoGalleryIndex = Math.max(0, Math.min(startIndex, photoGallery.length - 1));
      updatePhotoViewer();
      const overlay = $("photoViewer");
      if (!overlay) return;
      overlay.dataset.open = "1";
      overlay.setAttribute("aria-hidden", "false");
      overlay.classList.add("open");
    }
    function updatePhotoViewer() {
      const image = $("photoViewerImage");
      const counter = $("photoCounter");
      const prev = $("photoPrev");
      const next = $("photoNext");
      if (!image) return;
      const url = photoGallery[photoGalleryIndex] || "";
      image.src = url;
      image.alt = `Photo ${photoGalleryIndex + 1}`;
      if (counter) counter.textContent = photoGallery.length ? `${photoGalleryIndex + 1} / ${photoGallery.length}` : "";
      if (prev) prev.style.display = photoGallery.length > 1 ? "grid" : "none";
      if (next) next.style.display = photoGallery.length > 1 ? "grid" : "none";
    }
    function movePhoto(direction) {
      if (!photoGallery.length) return;
      photoGalleryIndex = (photoGalleryIndex + direction + photoGallery.length) % photoGallery.length;
      updatePhotoViewer();
    }
    function closePhotoViewer() {
      const overlay = $("photoViewer");
      const image = $("photoViewerImage");
      if (!overlay || !image) return;
      overlay.classList.remove("open");
      overlay.dataset.open = "0";
      overlay.setAttribute("aria-hidden", "true");
      image.src = "";
      image.alt = "";
      photoGallery = [];
      photoGalleryIndex = 0;
    }
    function renderListing(item) {
      const displayIndex = item.display_index || "";
      const isFavorite = favorites.has(String(item.id));
      const isHome = (item.category || selectedCategory()) === "home";
      const facts = [item.price_label, ...(item.details || []), isHome && item.area_m2 ? `${item.area_m2} m2` : "", isHome ? item.rooms_label : "", isHome && item.furniture_label ? `Furniture: ${item.furniture_label}` : "", item.location, item.distance_km != null ? `${item.distance_km} km` : "", isHome && item.rent_value ? `rent/admin +${item.rent_value} PLN` : "", isHome && item.total_known_cost ? `known total ${item.total_known_cost} PLN` : "", item.is_saved_favorite ? "saved favorite" : ""].filter(Boolean);
      const photos = (item.photos || []).map((url, photoIndex) => `<button type="button" class="photo-thumb" data-id="${escapeAttr(item.id)}" data-index="${photoIndex + 1}"><img src="${escapeAttr(url)}" loading="lazy" alt="${escapeAttr(item.title_en || item.title)}"></button>`).join("");
      const photoBlock = photos ? `<div class="photo-strip">${photos}</div>` : "";
      const description = item.description_en || item.description_short || "";
      const descriptionBlock = description ? `<div class="description">${escapeHtml(description)}</div>` : "";
      const costs = (item.cost_items || []).map((cost) => `<div class="cost-item"><b>${escapeHtml(cost.kind || "Cost")}</b>: ${escapeHtml(cost.text_en || cost.text || "")}</div>`).join("");
      const costBlock = isHome && costs ? `<div class="cost-box"><div class="cost-title">Additional / included costs found in description</div>${costs}</div>` : "";
      return `<article class="listing ${item.is_new ? "new" : ""} ${isFavorite ? "favorite" : ""}" data-id="${escapeAttr(item.id)}"><div class="title-line"><span class="num-badge">${displayIndex}</span><h2>${escapeHtml(item.title_en || item.title)}</h2><button type="button" class="favorite-toggle ${isFavorite ? "active" : ""}" data-id="${escapeAttr(item.id)}" title="Favorite">${isFavorite ? "★" : "☆"}</button></div>${photoBlock}<div class="facts">${facts.map((fact) => `<span class="fact">${escapeHtml(String(fact))}</span>`).join("")}</div>${costBlock}${descriptionBlock}<div class="listing-actions"><button type="button" class="open-link" data-url="${escapeAttr(item.url)}">Open on OLX</button></div></article>`;
    }
    function ensureMap() {
      if (map || !window.L) return;
      map = L.map("map", {scrollWheelZoom: true}).setView([50.07567, 19.93084], 12);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "&copy; OpenStreetMap"}).addTo(map);
      markerLayer = L.layerGroup().addTo(map);
    }
    function updateMap(listings, center) {
      ensureMap(); if (!map || !markerLayer) return;
      markerLayer.clearLayers(); if (radiusLayer) { radiusLayer.remove(); radiusLayer = null; }
      activeMarkerById = new Map(); const bounds = [];
      const selectedCity = cities.find((city) => city.slug === $("city_slug").value) || cities[0] || {lat: 50.07567, lon: 19.93084};
      const fallback = center && center.lat && center.lon ? center : selectedCity;
      const coordCounts = new Map();
      if (center && center.lat && center.lon) {
        const centerLatLng = [center.lat, center.lon]; bounds.push(centerLatLng);
        L.circleMarker(centerLatLng, {radius: 8, color: "#e0a33a", fillColor: "#e0a33a", fillOpacity: 0.95}).addTo(markerLayer).bindPopup("Search center");
        if (center.radius_km) radiusLayer = L.circle(centerLatLng, {radius: center.radius_km * 1000, color: "#e0a33a", weight: 1, fillColor: "#e0a33a", fillOpacity: 0.08}).addTo(map);
      }
      listings.forEach((item) => {
        let lat = Number(item.lat), lon = Number(item.lon), approximate = false;
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
          lat = Number(fallback.lat); lon = Number(fallback.lon); approximate = true;
        }
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        const key = `${lat.toFixed(5)},${lon.toFixed(5)}`;
        const duplicateIndex = coordCounts.get(key) || 0;
        coordCounts.set(key, duplicateIndex + 1);
        if (duplicateIndex > 0 || approximate) {
          const angle = duplicateIndex * 2.399963229728653;
          const ring = Math.floor(duplicateIndex / 8) + 1;
          const distance = approximate ? 0.00045 * ring : 0.00022 * ring;
          lat += Math.sin(angle) * distance;
          lon += (Math.cos(angle) * distance) / Math.max(Math.cos(lat * Math.PI / 180), 0.2);
        }
        const latLng = [lat, lon]; bounds.push(latLng);
        const displayIndex = item.display_index || "";
        const marker = L.marker(latLng, {
          icon: L.divIcon({
            className: "",
            html: `<div class="map-pin ${item.is_new ? "new" : ""} ${approximate ? "approx" : ""}">${displayIndex}</div>`,
            iconSize: [30, 30],
            iconAnchor: [15, 15]
          })
        }).addTo(markerLayer);
        const approxText = approximate ? "<br><small>Approximate location: city/search center was used</small>" : "";
        marker.bindPopup(`<b>#${displayIndex} ${escapeHtml(item.title_en || item.title)}</b><br>${escapeHtml(item.price_label || "")}${approxText}`);
        marker.on("click", () => focusListing(item.id)); activeMarkerById.set(String(item.id), marker);
      });
      if (bounds.length > 1) map.fitBounds(bounds, {padding: [24, 24], maxZoom: 14});
      else if (bounds.length === 1) map.setView(bounds[0], 13);
      else if (selectedCity) map.setView([selectedCity.lat, selectedCity.lon], 12);
      setTimeout(() => map.invalidateSize(), 50);
    }
    function focusListing(id) {
      document.querySelectorAll(".listing").forEach((card) => card.classList.toggle("active", card.dataset.id === String(id)));
      const marker = activeMarkerById.get(String(id)); if (marker && map) { map.setView(marker.getLatLng(), Math.max(map.getZoom(), 14)); marker.openPopup(); }
    }
    function escapeHtml(value) { return String(value).replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"}[char])); }
    function escapeAttr(value) { return escapeHtml(value); }
    async function saveConfig() {
      setBusy(true); $("status").textContent = "Saving filters...";
      try { await api("/api/config", collectPayload()); $("status").textContent = "Filters were saved to config.json."; }
      catch (error) { $("status").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`; }
      finally { setBusy(false); }
    }
    async function resetSeen() {
      setBusy(true); $("status").textContent = "Resetting memory...";
      try { const data = await api("/api/seen/reset", collectPayload()); $("status").textContent = data.removed ? "Seen-listing memory was reset for this filter." : "There was no saved memory for this filter."; }
      catch (error) { $("status").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`; }
      finally { setBusy(false); }
    }
    function toggleWatch() {
      if (watchTimer) { clearInterval(watchTimer); watchTimer = null; $("watch").textContent = "Start watching"; $("status").textContent = "Watching stopped."; return; }
      $("hide_seen").checked = true; const minutes = Math.max(Number($("scan_interval_minutes").value || 10), 1);
      scan(); watchTimer = setInterval(() => scan(), minutes * 60 * 1000); $("watch").textContent = "Stop watching"; $("status").textContent = `New-listing scans will run every ${minutes} minute(s).`;
    }
    $("scan").addEventListener("click", () => scan());
    $("save").addEventListener("click", saveConfig);
    $("resetSeen").addEventListener("click", resetSeen);
    $("watch").addEventListener("click", toggleWatch);
    $("sendTelegram").addEventListener("click", sendCurrentScanToTelegram);
    $("geocode").addEventListener("click", geocodeAddress);
    $("showAll").addEventListener("click", () => { $("hide_seen").checked = false; scan(); });
    $("showNew").addEventListener("click", () => { $("hide_seen").checked = true; scan(); });
    $("showFavorites").addEventListener("click", () => { favoriteMode = !favoriteMode; renderCurrentListings(); });
    $("photoViewer").addEventListener("click", (event) => { if (event.target.id === "photoViewer") closePhotoViewer(); });
    $("closePhotoViewer").addEventListener("click", closePhotoViewer);
    $("photoPrev").addEventListener("click", () => movePhoto(-1));
    $("photoNext").addEventListener("click", () => movePhoto(1));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closePhotoViewer();
      if (event.key === "ArrowLeft") movePhoto(-1);
      if (event.key === "ArrowRight") movePhoto(1);
    });
    document.querySelectorAll("input[name='category']").forEach((radio) => radio.addEventListener("change", async () => { setCategory(radio.value); $("center_lat").value = ""; $("center_lon").value = ""; $("district_id").value = ""; await loadDistricts(); updateMap([], null); }));
    $("city_slug").addEventListener("change", async () => { $("center_lat").value = ""; $("center_lon").value = ""; $("addressHint").textContent = "City changed; find the address again if you want to use an address radius."; await loadDistricts(); updateMap([], null); });
    setCategory("home");
    updateFavoriteControl();
    api("/api/config").then(async (data) => { populateCities(data.cities); applyConfig(data.config); await loadDistricts(data.config.district_id); ensureMap(); updateMap([], null); updateFavoriteControl(); }).catch((error) => { $("status").innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`; });
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Scan OLX.pl listings and notify on new matches.",
    )
    parser.set_defaults(config=DEFAULT_CONFIG_PATH, state=DEFAULT_STATE_PATH)
    add_common_args(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a starter config file.")
    add_common_args(init_parser)
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config.")

    configure_parser = subparsers.add_parser("configure", help="Configure filters interactively in the terminal.")
    add_common_args(configure_parser)

    scan_parser = subparsers.add_parser("scan", help="Run one scan now.")
    add_common_args(scan_parser)
    scan_parser.add_argument(
        "--notify-current",
        action="store_true",
        help="Notify for all current matches, even if already seen.",
    )
    scan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print results without writing seen-state.",
    )

    watch_parser = subparsers.add_parser("watch", help="Run periodic scans. Type 'scan' for an immediate scan.")
    add_common_args(watch_parser)

    serve_parser = subparsers.add_parser("serve", help="Run the localhost web interface.")
    add_common_args(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind. Default: 8000")

    url_parser = subparsers.add_parser("url", help="Print the OLX search URL generated from config.")
    add_common_args(url_parser)

    return parser.parse_args()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to config JSON.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=argparse.SUPPRESS,
        help="Path to seen-state JSON.",
    )


def main() -> None:
    configure_stdio()
    args = parse_args()

    if args.command == "init":
        write_default_config(args.config, overwrite=args.force)
        return
    if args.command == "configure":
        configure_interactively(args.config)
        return

    config = load_config(args.config)

    if args.command == "url":
        print(build_search_url(config))
        return
    if args.command == "scan":
        scan_once(config, args.state, notify_current=args.notify_current, dry_run=args.dry_run)
        return
    if args.command == "watch":
        watch(config, args.state)
        return
    if args.command == "serve":
        serve_web(args.config, args.state, args.host, args.port)
        return

    raise SystemExit(f"Unknown command: {args.command}")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


if __name__ == "__main__":
    main()
