"""Read recent bird sightings from the eBird API 2.0 (Cornell Lab of Ornithology).

eBird answers in whole checklists: one row per species per checklist. We trim each row to the
fields a birdwatcher needs and add a link to its eBird page, so every fact can be cited.
"""

import os

import httpx

API_URL = "https://api.ebird.org/v2"
# The API refuses anything further back than this.
MAX_DAYS_BACK = 30
# The API's widest search radius, in km.
MAX_DIST_KM = 50


def _get(path: str, **params) -> list | dict:
    if not os.environ.get("EBIRD_API_KEY"):
        raise RuntimeError("Set EBIRD_API_KEY in this project's .env file.")
    response = httpx.get(
        API_URL + path,
        params={k: v for k, v in params.items() if v is not None},
        headers={"X-eBirdApiToken": os.environ["EBIRD_API_KEY"]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _sighting(row: dict) -> dict:
    """One eBird row -> the fields we keep. howMany is missing when the observer only noted presence."""
    return {
        "species": row["comName"],
        "scientific_name": row["sciName"],
        "species_url": f"https://ebird.org/species/{row['speciesCode']}",
        "count": row.get("howMany"),
        "seen_at": row["obsDt"],
        "location": row["locName"],
        "location_id": row["locId"],
        # A private location is one observer's own pin, not a public hotspot with a page.
        "is_hotspot": not row.get("locationPrivate", False),
        "lat": row["lat"],
        "lng": row["lng"],
        "source": f"https://ebird.org/checklist/{row['subId']}",
    }


def nearby_hotspots(lat: float, lng: float, dist_km: float = 3, days_back: int = 14) -> list[dict]:
    """Public birding spots within dist_km that had a checklist in the last days_back days."""
    rows = _get("/ref/hotspot/geo", lat=lat, lng=lng, dist=min(dist_km, MAX_DIST_KM), back=min(days_back, MAX_DAYS_BACK), fmt="json")
    return [
        {
            "location": row["locName"],
            "location_id": row["locId"],
            "region": row.get("subnational2Code"),
            "lat": row["lat"],
            "lng": row["lng"],
            "species_all_time": row.get("numSpeciesAllTime"),
            "latest_checklist": row.get("latestObsDt"),
            "source": f"https://ebird.org/hotspot/{row['locId']}",
        }
        for row in rows
    ]


def recent_at_location(location_id: str, days_back: int = 14) -> list[dict]:
    """The latest sighting of each species at one location, newest first."""
    rows = _get(f"/data/obs/{location_id}/recent", back=min(days_back, MAX_DAYS_BACK))
    return sorted((_sighting(row) for row in rows), key=lambda s: s["seen_at"], reverse=True)


def recent_nearby(lat: float, lng: float, dist_km: float = 3, days_back: int = 14) -> list[dict]:
    """The latest sighting of each species within dist_km, newest first."""
    rows = _get("/data/obs/geo/recent", lat=lat, lng=lng, dist=min(dist_km, MAX_DIST_KM), back=min(days_back, MAX_DAYS_BACK))
    return sorted((_sighting(row) for row in rows), key=lambda s: s["seen_at"], reverse=True)


def notable_nearby(lat: float, lng: float, dist_km: float = 5, days_back: int = 14) -> list[dict]:
    """Sightings eBird flags as unusual for the place or the date, one entry per species and location.

    A rare bird draws a crowd: one oriole in one park was 404 rows on 174 checklists. Grouping keeps
    the crowd as a number instead of 404 lines of context.
    """
    rows = _get("/data/obs/geo/recent/notable", lat=lat, lng=lng, dist=min(dist_km, MAX_DIST_KM), back=min(days_back, MAX_DAYS_BACK), detail="full")
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row["speciesCode"], row["locId"]), []).append(row)
    notable = []
    for group in groups.values():
        latest = max(group, key=lambda row: row["obsDt"])
        notable.append(
            {
                **_sighting(latest),
                "first_seen_at": min(row["obsDt"] for row in group),
                "checklists": len({row["subId"] for row in group}),
                # eBird's volunteer reviewers accept or reject unusual reports. Unreviewed ones are still pending.
                "confirmed_by_reviewer": any(row.get("obsReviewed") and row.get("obsValid") for row in group),
            }
        )
    return sorted(notable, key=lambda s: (s["checklists"], s["seen_at"]), reverse=True)


def sightings_on_date(region: str, year: int, month: int, day: int) -> list[dict]:
    """Every species reported in a region on one past day. Unlike the 'recent' calls, this has no
    30-day limit, so a question about a fixed date keeps the same answer.

    region is an eBird region code, e.g. 'US-NY-061' for New York County (Manhattan).
    """
    rows = _get(f"/data/obs/{region}/historic/{year}/{month}/{day}")
    return [_sighting(row) for row in rows]


if __name__ == "__main__":
    # Smoke test: uv run ebird.py
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
    columbia = {"lat": 40.8075, "lng": -73.9626}

    hotspots = nearby_hotspots(**columbia)
    assert hotspots and hotspots[0]["source"].startswith("https://ebird.org/hotspot/"), "no hotspots"
    busiest = max(hotspots, key=lambda h: h["species_all_time"] or 0)
    print(len(hotspots), "hotspots within 3 km, e.g.", busiest["location"], "|", busiest["species_all_time"], "species all time")

    here = recent_at_location(busiest["location_id"])
    assert here and here[0]["source"].startswith("https://ebird.org/checklist/"), "no sightings at the hotspot"
    print(len(here), "species there in 14 days, e.g.", here[0]["species"], "|", here[0]["seen_at"], "|", here[0]["source"])

    nearby = recent_nearby(**columbia)
    assert nearby, "no recent sightings"
    print(len(nearby), "species within 3 km in 14 days")

    notable = notable_nearby(**columbia)
    assert notable and notable[0]["checklists"] >= 1, "no notable sightings"
    top = notable[0]
    print(len(notable), "notable species-and-place entries, e.g.", top["species"], "at", top["location"], "|", top["checklists"], "checklists | confirmed:", top["confirmed_by_reviewer"])

    past = sightings_on_date("US-NY-061", 2026, 5, 10)
    assert past, "no sightings on the past date"
    print(len(past), "species in Manhattan on 2026-05-10")
