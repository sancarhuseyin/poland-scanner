import datetime as dt
from urllib.parse import parse_qs, urlparse

import home_scanner as hs


def make_listing(**overrides):
    data = {
        "id": "listing-1",
        "title": "Sunny flat near park",
        "url": "https://www.olx.pl/d/oferta/test.html",
        "price_value": 2000.0,
        "price_label": "2 000 zl",
        "rent_value": 400.0,
        "area_m2": 38.0,
        "rooms_key": "two",
        "rooms_label": "2 pokoje",
        "furniture_key": "yes",
        "furniture_label": "Umeblowane",
        "location": "Krakow, Krowodrza, Malopolskie",
        "district": "Krowodrza",
        "lat": 50.0879,
        "lon": 19.9530,
        "map_radius": None,
        "created_time": "",
        "refresh_time": "",
        "description": "Bright apartment with balcony",
        "photos": ["https://img.example/photo.jpg"],
        "cost_items": [],
        "details": [],
        "has_photo": True,
    }
    data.update(overrides)
    return hs.Listing(**data)


def test_build_search_url_home_filters():
    config = {
        "ui": {"category": "home"},
        "olx_filters": {
            "city_slug": "krakow",
            "query": "stare miasto",
            "sort": "filter_float_price:asc",
            "owner_type": "private",
            "price_to": 2500,
            "area_from": 30,
            "rooms": ["2", "three"],
            "furniture": ["yes"],
            "only_with_photo": True,
        },
    }

    url = hs.build_search_url(config)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.path == "/nieruchomosci/mieszkania/wynajem/krakow/q-stare%20miasto/"
    assert params["search[order]"] == ["filter_float_price:asc"]
    assert params["search[private_business]"] == ["private"]
    assert params["search[filter_float_price:to]"] == ["2500"]
    assert params["search[filter_float_m:from]"] == ["30"]
    assert params["search[photos]"] == ["1"]
    assert params["search[filter_enum_rooms][0]"] == ["two"]
    assert params["search[filter_enum_rooms][1]"] == ["three"]
    assert params["search[filter_enum_furniture][0]"] == ["yes"]


def test_build_search_url_car_filters_omit_home_specific_params():
    config = {
        "ui": {"category": "car"},
        "olx_filters": {
            "city_slug": "warszawa",
            "sort": "filter_float_price:desc",
            "price_from": 10000,
            "year_from": 2015,
            "mileage_to": 150000,
            "area_from": 40,
            "rooms": ["two"],
            "only_with_photo": False,
        },
    }

    url = hs.build_search_url(config)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.path == "/motoryzacja/samochody/warszawa/"
    assert params["search[filter_float_price:from]"] == ["10000"]
    assert params["search[filter_float_year:from]"] == ["2015"]
    assert params["search[filter_float_milage:to]"] == ["150000"]
    assert "search[filter_float_m:from]" not in params
    assert not any(key.startswith("search[filter_enum_rooms]") for key in params)


def test_parse_number_handles_common_price_formats():
    assert hs.parse_number("2 500 zl") == 2500.0
    assert hs.parse_number("2.500 zl") == 2500.0
    assert hs.parse_number("1.234,50 zl") == 1234.5
    assert hs.parse_number("1,234.50") == 1234.5
    assert hs.parse_number("5,7 l/100km") == 5.7
    assert hs.parse_number("no number") is None


def test_total_known_cost_prefers_explicit_total_at_or_above_base_price():
    listing = make_listing(
        price_value=2000.0,
        rent_value=500.0,
        cost_items=[
            {"kind": "Total", "amount_value": 1900},
            {"kind": "Total", "amount_value": 2300},
            {"kind": "Total", "amount_value": 2500},
        ],
    )

    assert listing.total_known_cost == 2300.0


def test_total_known_cost_falls_back_to_rent_when_no_valid_explicit_total():
    listing = make_listing(
        price_value=2000.0,
        rent_value=500.0,
        cost_items=[{"kind": "Total", "amount_value": 1900}],
    )

    assert listing.total_known_cost == 2500.0


def test_listing_matches_combines_photo_price_text_total_and_radius_filters():
    listing = make_listing()
    filters = {
        "category": "home",
        "require_photo": True,
        "min_price": 1800,
        "max_price": 2200,
        "min_area_m2": 35,
        "max_area_m2": 45,
        "rooms": ["2"],
        "furniture": ["yes"],
        "apply_total_limit": True,
        "max_total_known_cost": 2500,
        "center_lat": 50.0879,
        "center_lon": 19.9530,
        "radius_km": 1,
        "apply_radius_filter": True,
        "keywords_all": ["sunny", "balcony"],
        "exclude_keywords": ["basement"],
        "districts_any": ["krowodrza"],
    }

    assert hs.listing_matches(listing, filters)

    assert not hs.listing_matches(listing, {**filters, "exclude_keywords": ["balcony"]})
    assert not hs.listing_matches(listing, {**filters, "center_lat": 0, "center_lon": 0})


def test_update_seen_returns_only_new_items():
    bucket = {"seen": {}}
    first = make_listing(id="1", title="First")
    second = make_listing(id="2", title="Second")
    third = make_listing(id="3", title="Third")

    assert hs.update_seen(bucket, [first, second]) == [first, second]
    assert hs.update_seen(bucket, [first, third]) == [third]
    assert set(bucket["seen"]) == {"1", "2", "3"}
    assert "last_scan" in bucket


def test_prune_seen_removes_entries_older_than_ttl():
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).replace(microsecond=0).isoformat()
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).replace(microsecond=0).isoformat()
    bucket = {
        "seen": {
            "old": {"first_seen": old},
            "fresh": {"first_seen": fresh},
            "unknown": {"first_seen": "not-a-date"},
        }
    }

    hs.prune_seen(bucket, ttl_days=60)

    assert set(bucket["seen"]) == {"fresh", "unknown"}


def test_ui_payload_to_config_normalizes_car_fields():
    config = hs.ui_payload_to_config(
        {
            "category": "car",
            "city_slug": "warszawa",
            "sort": "known_total:asc",
            "price_to": "30000",
            "area_from": "40",
            "rooms": ["two"],
            "year_from": "2015",
            "mileage_to": "150000",
            "only_with_photo": True,
        },
        {"notifications": {"console": True}},
    )

    assert config["ui"] == {"sort_mode": "filter_float_price:asc", "category": "car"}
    assert config["olx_filters"]["category_path"] == "motoryzacja/samochody"
    assert config["olx_filters"]["area_from"] is None
    assert config["olx_filters"]["rooms"] == []
    assert config["olx_filters"]["year_from"] == 2015.0
    assert config["olx_filters"]["mileage_to"] == 150000.0
    assert config["local_filters"]["category"] == "car"


def test_build_scan_payload_message_formats_displayed_results():
    message = hs.build_scan_payload_message(
        [
            {
                "title": "Flat near park",
                "price_label": "2 000 PLN",
                "area_m2": 38,
                "rooms_label": "2 rooms",
                "location": "Krakow, Krowodrza",
                "distance_km": 1.4,
                "total_known_cost": 2400,
                "url": "https://www.olx.pl/d/oferta/test.html",
            }
        ],
        total_count=4,
        displayed_count=3,
    )

    assert "home-scanner: 3 displayed listing(s), 4 total match(es)" in message
    assert "01. Flat near park | 2 000 PLN | 38 m2 | 2 rooms | Krakow, Krowodrza | 1.4 km | known total 2400 PLN" in message
    assert "https://www.olx.pl/d/oferta/test.html" in message
    assert "...and 2 more displayed listing(s)." in message
