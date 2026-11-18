"""Read bird observations from the iNaturalist API. No key needed.

iNaturalist is the second opinion on eBird: its observations are mostly photos that other users
identify, so each one has its own page to cite. It also keeps every year, which eBird's 30-day
'recent' calls don't, so it can say which months a species usually turns up in.
"""

from datetime import date, timedelta

import httpx

API_URL = "https://api.inaturalist.org/v1"
HEADERS = {"User-Agent": "bird-side-quests/0.1 (student course project)"}
BIRDS = 3
MAX_OBSERVATIONS = 200


def _get(path: str, **params) -> dict:
    response = httpx.get(
        API_URL + path,
        params={"verifiable": "true", **{k: v for k, v in params.items() if v is not None}},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def window(days_back: int, until: date | None) -> dict:
    """The d1 and d2 parameters for the days_back days ending on until (today by default)."""
    until = until or date.today()
    return {"d1": (until - timedelta(days=days_back)).isoformat(), "d2": until.isoformat()}


def _observation(row: dict) -> dict:
    taxon = row.get("taxon") or {}
    # Five decimals is about a metre: plenty, and shorter than the fourteen the API sends.
    lat, lng = (round(float(x), 5) for x in row["location"].split(",")) if row.get("location") else (None, None)
    # Only a photo whose owner chose a licence may be shown. No licence means all rights reserved.
    photo = next((p for p in row.get("photos", []) if p.get("license_code")), None)
    return {
        "species": taxon.get("preferred_common_name") or taxon.get("name"),
        "scientific_name": taxon.get("name"),
        "seen_on": row.get("observed_on"),
        "location": row.get("place_guess"),
        "lat": lat,
        "lng": lng,
        # iNaturalist blurs the coordinates of threatened species, and of observers who ask.
        "location_obscured": bool(row.get("obscured")),
        # 'research': the community agrees on the species. 'needs_id': not yet.
        "quality_grade": row.get("quality_grade"),
        "photo_url": photo["url"].replace("square", "medium") if photo else None,
        "photo_credit": photo.get("attribution") if photo else None,
        "source": row.get("uri"),
    }


def recent_observations(lat: float, lng: float, radius_km: float = 3, days_back: int = 14, until: date | None = None) -> dict:
    """Bird observations within radius_km in the days_back days ending on until, newest first:
    {total, observations}. total counts every match; observations holds the newest MAX_OBSERVATIONS."""
    body = _get(
        "/observations",
        lat=lat,
        lng=lng,
        radius=radius_km,
        taxon_id=BIRDS,
        hrank="species",  # skip observations only identified as far as 'a gull' or 'a bird'
        order_by="observed_on",
        per_page=MAX_OBSERVATIONS,
        **window(days_back, until),
    )
    return {"total": body["total_results"], "observations": [_observation(row) for row in body["results"]]}


def species_counts(lat: float, lng: float, radius_km: float = 3, days_back: int = 14, until: date | None = None) -> list[dict]:
    """How many observations each bird species had within radius_km in the window, most first."""
    dates = window(days_back, until)
    body = _get("/observations/species_counts", lat=lat, lng=lng, radius=radius_km, taxon_id=BIRDS, per_page=500, **dates)
    # The source is the list of exactly the observations counted: same species, place and dates.
    where = f"lat={lat}&lng={lng}&radius={radius_km}&d1={dates['d1']}&d2={dates['d2']}"
    return [
        {
            "species": row["taxon"].get("preferred_common_name") or row["taxon"]["name"],
            "scientific_name": row["taxon"]["name"],
            "observations": row["count"],
            "source": f"https://www.inaturalist.org/observations?taxon_id={row['taxon']['id']}&{where}",
        }
        for row in body["results"]
    ]


def _find_taxon(name: str) -> dict:
    """The iNaturalist record of the bird species with exactly this common or scientific name.

    eBird and iNaturalist don't always agree on names. Guessing the nearest match would report one
    species' history as another's, so anything but an exact match raises LookupError.
    """
    body = _get("/taxa", q=name, taxon_id=BIRDS, rank="species", is_active="true", per_page=10, verifiable=None)
    wanted = name.strip().lower()
    for taxon in body["results"]:
        if wanted in (taxon["name"].lower(), (taxon.get("preferred_common_name") or "").lower()):
            return taxon
    close = ", ".join(t.get("preferred_common_name") or t["name"] for t in body["results"][:5]) or "none"
    raise LookupError(f"No bird species on iNaturalist is named exactly '{name}'. Closest: {close}.")


def find_bird(name: str) -> dict:
    """Match a common or scientific name to one iNaturalist bird species: {id, species, scientific_name}."""
    taxon = _find_taxon(name)
    return {"id": taxon["id"], "species": taxon.get("preferred_common_name") or taxon["name"], "scientific_name": taxon["name"]}


def bird_photo(name: str) -> dict | None:
    """The species' own photo on iNaturalist, for its card: {photo_url, photo_credit, source}.
    None when the photo's owner chose no licence, since then it may not be shown."""
    taxon = _find_taxon(name)
    photo = taxon.get("default_photo") or {}
    if not photo.get("license_code") or not photo.get("medium_url"):
        return None
    return {"photo_url": photo["medium_url"], "photo_credit": photo.get("attribution"), "source": f"https://www.inaturalist.org/taxa/{taxon['id']}"}


def monthly_history(name: str, lat: float, lng: float, radius_km: float = 25) -> dict:
    """How many observations of one species each calendar month has had within radius_km, all years
    combined. Shows whether a sighting is in season. The radius is wide on purpose: a single park
    has too few observations of most species to show a pattern.
    """
    bird = find_bird(name)
    body = _get("/observations/histogram", lat=lat, lng=lng, radius=radius_km, taxon_id=bird["id"], interval="month_of_year")
    months = body["results"]["month_of_year"]
    return {
        "species": bird["species"],
        "scientific_name": bird["scientific_name"],
        "radius_km": radius_km,
        "observations_by_month": {int(month): count for month, count in months.items()},
        "total_observations": sum(months.values()),
        "source": f"https://www.inaturalist.org/observations?taxon_id={bird['id']}&lat={lat}&lng={lng}&radius={radius_km}",
    }


if __name__ == "__main__":
    # Smoke test: uv run inaturalist.py
    columbia = {"lat": 40.8075, "lng": -73.9626}

    recent = recent_observations(**columbia)
    seen = recent["observations"]
    assert seen and seen[0]["source"].startswith("https://www.inaturalist.org/observations/"), "no observations"
    newest = seen[0]
    print(recent["total"], "observations within 3 km in 14 days, e.g.", newest["species"], "|", newest["seen_on"], "|", newest["source"])
    print(sum(1 for s in seen if s["photo_url"]), "have a photo we may show |", sum(1 for s in seen if s["location_obscured"]), "have a blurred location")

    counts = species_counts(**columbia)
    assert counts and counts[0]["observations"] >= 1, "no species counts"
    print(len(counts), "species, most observed:", [(c["species"], c["observations"]) for c in counts[:3]])

    history = monthly_history("Bullock's Oriole", **columbia)
    assert len(history["observations_by_month"]) == 12, "histogram is not twelve months"
    print(history["species"], "by month within 25 km:", history["observations_by_month"], "|", history["source"])

    picture = bird_photo("Strix varia")
    print("Barred Owl photo:", picture)

    try:
        find_bird("oriole")
        raise AssertionError("a vague name matched one species")
    except LookupError as e:
        print("vague name refused:", e)
