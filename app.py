import asyncio
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import uvicorn
import litellm
from agents import Agent, MaxTurnsExceeded, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from openai.types.shared import Reasoning
from pydantic import BaseModel

import ebird
import inaturalist
from context_panel import AgentContexts, PanelHooks, add_context_panel

HERE = Path(__file__).parent

# EBIRD_API_KEY comes from the .env file next to this one. On Cloud Run it is set on the service instead.
load_dotenv(HERE / ".env")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes the call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers only read records and take notes: the cheaper model is enough.
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

WRITER_SETTINGS = ModelSettings(extra_args=SETTINGS.extra_args, reasoning=Reasoning(effort="low"))

MAX_RESEARCHERS = 6 
LEAD_TURNS = 12 
RESEARCHER_TURNS = 12
RESEARCH_SECONDS = 150 
REWRITE_BEFORE_SECONDS = 200


class RunLog(PanelHooks):
    """The context panel's hooks, plus a record of every tool call any agent made. The report's
    links are checked against it, so a link the model made up is caught."""

    def __init__(self, contexts: AgentContexts, emit):
        super().__init__(contexts, emit)
        self.tool_calls: list[dict] = []

    async def on_tool_end(self, context, agent, tool, result):
        try:
            args = json.loads(getattr(context, "tool_arguments", None) or "{}")
        except ValueError:
            args = {}
        self.tool_calls.append({"agent": agent.name, "name": tool.name, "args": args, "result": str(result)})
        if tool.name == "find_birding_spots":
            # The page draws the search on its map as it happens: where it starts from, and the
            # spots there are to choose between.
            try:
                spots = json.loads(str(result)).get("spots") or []
            except ValueError:
                spots = []
            if spots:
                nearby = [{"location_id": s["location_id"], "spot": s["location"], "lat": s["lat"], "lng": s["lng"], "distance_km": s["distance_km"]} for s in spots]
                self.emit({"type": "stage", "step": "start", "lat": args.get("lat"), "lng": args.get("lng"), "spots": nearby})


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    hooks: RunLog
    request: str = ""  # everything the user has said, to check a date against
    researchers: int = 0  # how many researchers have started, to number them
    findings: list[str] = field(default_factory=list)  # what each researcher reported, for the Writer
    unchecked: list[str] = field(default_factory=list)  # spots the Lead wanted checked but nobody could
    checked: dict[str, str] = field(default_factory=dict)  # location_id -> name of each spot a researcher reported on


# --- Tools ---
#
# Every tool returns JSON text. Each record carries a "source" link, so the Writer can cite it.
# Lists are cut to a limit, with "not_shown" saying how many were left out: a busy park would
# otherwise crowd the context.

MAX_SPOTS = 30
MAX_SIGHTINGS = 80
MAX_UNUSUAL = 40
MAX_OBSERVATIONS = 40
MAX_ON_DATE = 150


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line distance between two points, in km. Not a walking route: we have no street data."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(a)), 2)


def _answer(what: str, build) -> str:
    # The model cannot see an exception. Return something it can reason about.
    try:
        return json.dumps(build(), ensure_ascii=False)
    except httpx.HTTPStatusError as e:
        # The source refused the request, usually over a bad argument. Pass on what it said.
        said = " ".join(e.response.text.split())[:300]
        return json.dumps({"error": f"Could not get {what}: the source answered {e.response.status_code}. {said}"})
    except Exception as e:
        return json.dumps({"error": f"Could not get {what}: {type(e).__name__}: {e}"})


def _point(lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"({lat}, {lng}) is not a latitude and longitude.")


def _days(days_back: int) -> int:
    """eBird keeps 'recent' sightings for 30 days. The answer says which window was really used."""
    return max(1, min(int(days_back), ebird.MAX_DAYS_BACK))


def _keep(record: dict, *keys: str) -> dict:
    return {key: record[key] for key in keys if record.get(key) is not None}


def _cut(records: list, limit: int) -> dict:
    return {"not_shown": max(0, len(records) - limit)} if len(records) > limit else {}


@function_tool
def find_birding_spots(lat: float, lng: float, radius_km: float = 3) -> str:
    """List the public birding spots (eBird hotspots) near a point that had a checklist in the
    last 14 days, nearest first. Use it to choose where to look.

    Args:
        lat: Latitude of the starting point.
        lng: Longitude of the starting point.
        radius_km: How far to look, in km, up to 50.
    """

    def build():
        _point(lat, lng)
        spots = ebird.nearby_hotspots(lat, lng, radius_km, days_back=14)
        for spot in spots:
            spot["distance_km"] = distance_km(lat, lng, spot["lat"], spot["lng"])
        spots.sort(key=lambda spot: spot["distance_km"])
        if not spots:
            return {"spots": [], "note": f"No birding spots with a checklist in the last 14 days within {radius_km} km of ({lat}, {lng}). Check the coordinates or widen the radius."}
        return {"origin": {"lat": lat, "lng": lng}, "distance_is": "straight-line km", "spots": spots[:MAX_SPOTS], **_cut(spots, MAX_SPOTS)}

    return _answer("birding spots from eBird", build)


@function_tool
def sightings_at_spot(location_id: str, days_back: int = 14) -> str:
    """List the bird species reported on eBird at one birding spot, with the latest sighting of
    each, newest first.

    Args:
        location_id: The spot's eBird location_id from find_birding_spots, e.g. 'L1799559'.
        days_back: How many days to look back, 1 to 30.
    """

    def build():
        days = _days(days_back)
        sightings = ebird.recent_at_location(location_id, days)
        rows = [_keep(s, "species", "scientific_name", "count", "seen_at", "source") for s in sightings]
        place = sightings[0]["location"] if sightings else None
        return {
            "location": place,
            "location_id": location_id,
            "days_back": days,
            "species_count": len(rows),
            # Without this the model reads one row as one report, and writes "seen once".
            "note": "One row per species: its latest sighting only. This does not show how many times a species was reported.",
            "sightings": rows[:MAX_SIGHTINGS],
            **_cut(rows, MAX_SIGHTINGS),
        }

    return _answer(f"eBird sightings at {location_id}", build)


@function_tool
def unusual_sightings(lat: float, lng: float, radius_km: float = 5, days_back: int = 14) -> str:
    """List the sightings near a point that eBird flags as unusual for the place or the time of
    year, one entry per species and location. 'checklists' is how many separate reports there
    were, and 'confirmed_by_reviewer' says whether an eBird reviewer has accepted the report.

    Args:
        lat: Latitude of the starting point.
        lng: Longitude of the starting point.
        radius_km: How far to look, in km, up to 50.
        days_back: How many days to look back, 1 to 30.
    """

    def build():
        _point(lat, lng)
        days = _days(days_back)
        entries = ebird.notable_nearby(lat, lng, radius_km, days)
        rows = []
        for entry in entries:
            row = _keep(entry, "species", "scientific_name", "location", "location_id", "is_hotspot", "lat", "lng", "first_seen_at", "seen_at", "checklists", "confirmed_by_reviewer", "source")
            row["distance_km"] = distance_km(lat, lng, entry["lat"], entry["lng"])
            rows.append(row)
        return {"origin": {"lat": lat, "lng": lng}, "distance_is": "straight-line km", "days_back": days, "entries": rows[:MAX_UNUSUAL], **_cut(rows, MAX_UNUSUAL)}

    return _answer("unusual sightings from eBird", build)


@function_tool
def inaturalist_sightings(lat: float, lng: float, radius_km: float = 0.5, days_back: int = 14, until_date: str = "today") -> str:
    """List the bird observations on iNaturalist near a point: a count per species, then the
    individual observations, newest first. A second source to check eBird against. Unlike the eBird
    tools, it can look at any past period.

    Args:
        lat: Latitude of the point, e.g. a birding spot's.
        lng: Longitude of the point.
        radius_km: How far around the point to look, in km.
        days_back: How many days before until_date to include. 0 is until_date alone.
        until_date: The last day to include: 'today', or a date like '2026-11-07'.
    """

    def build():
        _point(lat, lng)
        until = date.today() if until_date.strip().lower() in ("", "today") else date.fromisoformat(until_date.strip())
        days = max(0, int(days_back))
        dates = inaturalist.window(days, until)
        species = inaturalist.species_counts(lat, lng, radius_km, days, until)
        recent = inaturalist.recent_observations(lat, lng, radius_km, days, until)
        rows = []
        for seen in recent["observations"]:
            row = _keep(seen, "species", "scientific_name", "seen_on", "location", "lat", "lng", "quality_grade", "photo_url", "photo_credit", "source")
            if seen["lat"] is not None:
                row["distance_km"] = distance_km(lat, lng, seen["lat"], seen["lng"])
            if seen["location_obscured"]:
                row["location_obscured"] = True
            rows.append(row)
        return {
            "origin": {"lat": lat, "lng": lng},
            "radius_km": radius_km,
            # Without this the model credits one park's rare bird to every spot within the circle.
            "note": "Everything within radius_km of the point, which can reach into a neighbouring spot. Each observation's 'location' and 'distance_km' show where it really was.",
            "window": dates,
            "source": f"https://www.inaturalist.org/observations?taxon_id={inaturalist.BIRDS}&lat={lat}&lng={lng}&radius={radius_km}&d1={dates['d1']}&d2={dates['d2']}",
            "species_count": len(species),
            "species": species[:MAX_SIGHTINGS],
            "observation_count": recent["total"],
            "observations": rows[:MAX_OBSERVATIONS],
            "not_shown": max(0, recent["total"] - MAX_OBSERVATIONS),
        }

    return _answer("observations from iNaturalist", build)


@function_tool
def season_history(species: str, lat: float, lng: float) -> str:
    """Count the iNaturalist observations of one bird species within 25 km of a point for each
    calendar month, all years combined. Shows whether the species is usual in the area at this
    time of year, or rare there at all.

    Args:
        species: The bird's exact common or scientific name, e.g. 'Barred Owl' or 'Strix varia'.
        lat: Latitude of the point.
        lng: Longitude of the point.
    """

    def build():
        _point(lat, lng)
        return inaturalist.monthly_history(species, lat, lng)

    return _answer(f"the season history of {species} from iNaturalist", build)


@function_tool
def sightings_on_date(place: str, on_date: str) -> str:
    """List every bird species reported on eBird at one birding spot, or in one whole county, on
    one past day. For questions about a specific date: unlike the 30-day eBird tools, it reaches
    any day in the past.

    Args:
        place: A spot's location_id from find_birding_spots, e.g. 'L1799559', for that spot alone. Or a county code, e.g. 'US-NY-061' for Manhattan, for everything reported in the county.
        on_date: The day, like '2026-11-05'.
    """

    def build():
        day = date.fromisoformat(on_date.strip())
        sightings = ebird.sightings_on_date(place.strip(), day.year, day.month, day.day)
        rows = [_keep(s, "species", "scientific_name", "count", "seen_at", "location", "location_id", "source") for s in sightings]
        return {
            "place": place.strip(),
            "date": day.isoformat(),
            "species_count": len(rows),
            # Without this the model reads one row as one checklist, and calls the sighting thin evidence.
            "note": "One row per species reported that day. This does not show how many checklists reported it.",
            "sightings": rows[:MAX_ON_DATE],
            **_cut(rows, MAX_ON_DATE),
        }

    return _answer(f"eBird sightings at {place} on {on_date}", build)


# --- The researcher: one birding spot, checked against both sources ---

researcher = Agent(
    name="Researcher",
    instructions="""\
You check one birding spot for a beginner birdwatcher, using records of recent sightings.

1. Call sightings_at_spot with the spot's location_id: what eBird users reported there.
2. Call inaturalist_sightings at the spot's latitude and longitude with radius_km 0.5: a second
   source to compare. It returns everything inside that circle, which can include a neighbouring
   spot's birds. Count an observation for your spot only if its location fits: otherwise say it
   was nearby, and where.
   Only if your prompt has a "Date asked about" line, replace steps 1 and 2: call
   sightings_on_date with the spot's location_id and that date, and inaturalist_sightings with
   until_date set to that date and days_back 0. Report the number of species eBird has for that
   day, and name them.
3. For at most 3 birds that stand out (the unusual ones named in your task, or ones a beginner
   would enjoy), call season_history to see whether they are usual here at this time of year.
   Make at most 6 tool calls in all.
4. Reply with the facts that answer your task and nothing else, at most 8 bullets. End every
   bullet with the source link of the record it came from, copied exactly from the tool result,
   e.g. ([eBird checklist](https://ebird.org/checklist/S409614007)). Never write a link that is
   not in a tool result.
   State only what the tool results show. Don't describe the place or the birds from memory.
   For each bird you report, say what each source has: the date of its latest eBird sighting, and
   how many iNaturalist observations it has in the window, or that iNaturalist has none. Give the
   number of eBird reports only when a tool result or your task states it. When your task lists
   sightings eBird flags as unusual at this spot, pass on each one's number of checklists, its
   dates and whether a reviewer confirmed it, with its link.
   End with one bullet on the spot as a whole: how many species each source has there, and
   whether that is enough to recommend it. Cite it with the spot's eBird page and the 'source' at
   the top of the inaturalist_sightings result. If the records don't answer part of the task, say so.
   A link must back the sentence it follows: don't attach one bird's link to another claim.""",
    model=RESEARCHER_MODEL,
    model_settings=SETTINGS,
    # sightings_on_date is added in research(), and only for a question about one past day
    tools=[sightings_at_spot, inaturalist_sightings, season_history],
)


def flagged_at(location_id: str, tool_calls: list[dict]) -> list[str]:
    """What eBird flags as unusual at one spot, one line each, from the Lead's last
    unusual_sightings result. Code hands these to the researcher and to the Writer in full: passed
    along by the models, '174 checklists, reviewer-confirmed' got dropped on the way, and the plan
    then called the best-attested bird in the park thin evidence."""
    for call in reversed(tool_calls):
        if call["name"] == "unusual_sightings":
            entries = [e for e in json.loads(call["result"]).get("entries", []) if e.get("location_id") == location_id]
            return [
                f"- {e['species']}: on {e['checklists']} eBird checklist(s) here, first {e['first_seen_at']}, latest {e['seen_at']}, "
                f"{'confirmed by an eBird reviewer' if e['confirmed_by_reviewer'] else 'not yet confirmed by an eBird reviewer'} ({e['source']})"
                for e in entries
            ]
    return []


RELATIVE_DAY = re.compile(r"\b(yesterday|ago|last (night|week|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b")


def past_day(text: str, request: str) -> str:
    """'2026-11-05' if text is a day before today that the user asked about, else ''.

    Left to itself the Lead filled this in from today's date, and then from the date of a sighting
    it had just read. The researchers then looked up one day instead of the last two weeks. So
    code decides: the day has to be in the past, and the user's own words have to name it
    ("November 5", "Nov 5th", "11/5") or point back in time ("yesterday", "three days ago").
    """
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        return ""
    if day >= date.today():
        return ""
    said = request.lower()
    names_month = day.strftime("%B").lower() in said or re.search(rf"\b{day.strftime('%b').lower()}\b", said)
    names_day = re.search(rf"\b{day.day}(st|nd|rd|th)?\b", said)
    numeric = re.search(rf"\b0?{day.month}[/-]0?{day.day}\b", said)
    return day.isoformat() if (names_month and names_day) or numeric or RELATIVE_DAY.search(said) else ""


@function_tool
async def research(ctx: RunContextWrapper[Research], task: str, spot: str, location_id: str, lat: float, lng: float, distance_km: float, on_date: str = "") -> str:
    """Send a researcher to check one birding spot against both sources. Returns its findings,
    each with the record it came from.

    Args:
        task: What to find out there, with everything the researcher needs: what the user hopes to see, the birds they have already seen, and any unusual sightings you saw at this spot.
        spot: The spot's name, from find_birding_spots.
        location_id: The spot's location_id, from find_birding_spots.
        lat: The spot's latitude, from find_birding_spots.
        lng: The spot's longitude, from find_birding_spots.
        distance_km: The spot's distance_km, from find_birding_spots.
        on_date: Only when the user asked about one specific past day: that day, like '2026-11-05'. For an outing now, leave it empty.
    """
    if ctx.context.researchers >= MAX_RESEARCHERS:
        ctx.context.unchecked.append(spot)
        return "No researchers left. Reply DONE."
    # What researcher.as_tool() does: run the researcher in a fresh context and return its answer.
    # Written out so each run gets a numbered copy, and its own column in the context panel.
    ctx.context.researchers += 1
    agent = researcher.clone(name=f"Researcher {ctx.context.researchers}")
    prompt = f"Task: {task}\nSpot: {spot}\nlocation_id: {location_id}\nLatitude: {lat}\nLongitude: {lng}\nThe spot's eBird page: https://ebird.org/hotspot/{location_id}"
    asked_about = past_day(on_date, ctx.context.request)
    if asked_about:
        # A question about one past day: the researcher gets the past-day lookup in place of the 14-day one
        agent = agent.clone(tools=[sightings_on_date, inaturalist_sightings, season_history])
        prompt += f"\nDate asked about: {asked_about}"
    flagged = flagged_at(location_id, ctx.context.hooks.tool_calls)
    if flagged and not asked_about:  # eBird's flags describe the last few weeks, not one past day
        prompt += "\nSightings eBird flags as unusual at this spot:\n" + "\n".join(flagged)
    hooks = ctx.context.hooks
    hooks.activity(agent.name, f"checking {spot}")  # the page labels the researcher with its spot
    # ...and puts a pin on its map that pulses until this researcher reports back
    pin = {"type": "stage", "agent": agent.name, "spot": spot, "location_id": location_id, "lat": lat, "lng": lng, "distance_km": distance_km}
    hooks.emit({**pin, "step": "spot"})
    try:
        result = await Runner.run(agent, prompt, context=ctx.context, hooks=hooks, max_turns=RESEARCHER_TURNS)
    except MaxTurnsExceeded:
        ctx.context.unchecked.append(spot)
        hooks.emit({**pin, "step": "failed"})
        return f"The researcher for {spot} ran out of turns before reporting. Treat this spot as unchecked."
    except Exception as e:
        ctx.context.unchecked.append(spot)
        hooks.emit({**pin, "step": "failed"})
        return f"The researcher for {spot} failed ({type(e).__name__}). Treat this spot as unchecked."
    hooks.emit({**pin, "step": "reported"})
    ctx.context.checked[location_id] = spot
    ctx.context.findings.append(f"Spot: {spot}, {distance_km} km in a straight line from the starting point\nTask: {task}\n\n{result.final_output}")
    return str(result.final_output)


# --- The lead: finds the user, picks spots, and fans out to researchers ---

lead = Agent(
    name="Lead",
    instructions="""\
You lead a research team that plans short birdwatching outings in New York City. The user says
where they are, how much time they have, and what they hope to see.

First check the request. Before any research you need:
- Where the user is: a place you can put on a map, such as a landmark, a park, a street corner or
  a neighborhood. "New York", a whole borough, "near me" or a place you don't know is not enough.
  A latitude and longitude is enough: the page sends one when the user picks a point on its map.
- How much time they have, when they are asking where to go. A question about what was seen
  somewhere on some date doesn't need it.
If the request is not about birds or where to see them, call no tools. Say in one or two sentences
that you plan birdwatching outings from recent sighting records, and what they could ask instead.
If either is missing, call no tools. Reply with one short question that asks for what is missing
and nothing else, and invite them to name birds they have already seen if they want something
new. If you have already asked and only the time is still missing, assume one hour and say so in
each research task.

Otherwise:

1. Work out where the user is. If they give a latitude and longitude, use those numbers exactly.
   Otherwise use the latitude and longitude you know for the place they say they are at, not a
   place they ask about. Then call find_birding_spots there. The nearest spots should be in or beside the place the
   user named. If they are not, your coordinates are wrong: correct them and call it again.
   Call unusual_sightings at the same point to see what is out of the ordinary nearby. Skip it
   when the user asks about a specific past date: it describes the last few weeks, not that day.
2. Choose 2 to 4 spots worth checking, or just the spots the user names: near enough for the
   time the user has, with a recent checklist or an unusual sighting. Only when the user asks
   about one specific past day, pass that day as on_date in each research call. For an outing
   now, leave on_date empty: it is never today's date, and never the date of a sighting. Distances are straight lines, so a walk
   is longer than it looks. Call research once per spot, all in the same turn, so the
   researchers work in parallel.
3. Read the findings and look for gaps:
   - a spot turned out to have few recent records,
   - only one of the two sources has a bird you would send the user to see, or
   - nothing found so far matches what the user hopes to see.
   For each gap, call research again for just that gap: another spot from your list, or the same
   spot with a sharper task. Do this at most twice. If the findings have no gap, don't.
4. When the request is covered, or a gap can't be closed, reply DONE and nothing else: a writer
   writes the plan and says what is still unknown.""",
    model=MODEL,
    model_settings=SETTINGS,
    tools=[find_birding_spots, unusual_sightings, research],
)

# --- The writer: one call, after the research ends ---

writer = Agent(
    name="Writer",
    instructions="""\
Write a short birdwatching outing plan in markdown for a beginner, using only the findings
you're given. The conversation shows what the user asked for.

- Start with a one-paragraph answer to what the user asked. For an outing: where to go and what
  to look for. For a question about the records (what was reported somewhere, which spot had
  more, whether a bird is usual): the answer itself, with its numbers.
- Then a section per spot, at most 5 sentences each, best spot first, with its distance. Leave
  out a spot that adds nothing. When the user asked what was reported on a date, give the number
  of species eBird has for that day and name up to 12 of them.
- Keep the links: end every sentence that states a fact with the link from its finding, copied
  exactly. Never write a link that is not in the findings, and never move a link to a different
  claim than the one it backed in the findings.
- For each bird you recommend, give the date of its latest eBird sighting, and say whether
  iNaturalist has it too: "both sources", "eBird only" or "iNaturalist only". For a sighting eBird
  flags as unusual, give its number of checklists and whether a reviewer confirmed it.
- One source only is weaker support, not a disagreement: iNaturalist has far fewer records than
  eBird. Never say the sources disagree because one of them has no record.
- Flag thin evidence where it applies, once per spot: a single report, nothing in the last week,
  or a report no reviewer has confirmed. A rare bird with many confirmed reports is not thin, and
  an answer about one past day is not thin for covering one day.
- Compare each spot's distance with the time the user has. A person walks about 1 km in 12 to 15
  minutes, and the route is longer than the straight line. If a spot is probably out of reach on
  foot in that time, say so in the first paragraph, as an estimate.
- The records describe the past. Never promise a bird will be there: say when it was last reported.
  Write dates like "Nov 10".
- End with a section "What the records don't show": what the user asked that the findings don't
  answer, and any spots the brief says could not be checked. If the findings are too thin to
  recommend anywhere, say that plainly instead of stretching them.
- Write plain, specific sentences. Don't claim more than the findings do: no "teeming",
  "paradise", "must-see", or "it is worth noting".""",
    model=MODEL,
    model_settings=WRITER_SETTINGS,
)

# True of every plan, so code adds it: the Writer can't forget it, and can't reword it into a promise.
DATA_NOTE = (
    "*About these records: sightings are reported by eBird and iNaturalist volunteers. They show where "
    "people looked and what they reported, not every bird that is there, so a spot with few records may "
    "only have had few visitors, and a bird reported yesterday may have moved on. iNaturalist has far "
    "fewer records than eBird, so a bird missing from it is common. eBird's recent "
    "sightings reach back 30 days at most. Distances are straight lines from the starting point, not "
    "walking routes.*"
)

# --- Citations: every link in the report must come from a tool result ---

LINK = re.compile(r"https?://[^\s)\]>\"'\\]+")


def starting_point(tool_calls: list[dict]) -> str:
    """Where the Lead searched from, and the nearest spot to it, read from its last
    find_birding_spots call: the Writer states it, so the user can see if the place was misread."""
    for call in reversed(tool_calls):
        if call["name"] == "find_birding_spots":
            spots = json.loads(call["result"]).get("spots") or []
            if spots:
                nearest = spots[0]
                return (
                    f"Starting point searched from: latitude {call['args'].get('lat')}, longitude {call['args'].get('lng')}. "
                    f"The nearest birding spot to it is {nearest['location']}, {nearest['distance_km']} km away ({nearest['source']})."
                )
    return ""


def links_not_in_data(text: str, tool_calls: list[dict]) -> list[str]:
    """The links in text that no data tool returned: made up, or miscopied."""
    known = set()
    for call in tool_calls:
        if call["name"] != "research":  # a researcher's findings are its own words, not data
            known.update(LINK.findall(call["result"]))
    return [link for link in dict.fromkeys(LINK.findall(text)) if link.rstrip(".,;:") not in known]


def without_links(text: str, links: list[str]) -> str:
    """Drop the given links: '[words](link)' becomes 'words', and a bare link goes."""
    for link in links:
        text = re.sub(r"\[([^\]]*)\]\(" + re.escape(link) + r"\)", r"\1", text)
        text = text.replace(link, "")
    return text


def as_transcript(conversation: list[dict]) -> str:
    """The conversation as text for the Writer. An earlier plan is left out: its links came from
    another run's tool results, so the Writer must not reuse them."""
    lines = []
    for message in conversation:
        if message["role"] == "user":
            lines.append(f"User: {message['content']}")
        else:
            lines.append(f"Assistant: {message['content'] if message.get('kind') == 'question' else '(an earlier plan)'}")
    return "\n".join(lines)


async def deep_research(conversation: list[dict], hooks: RunLog) -> tuple[str, str]:
    """Answer the newest message of a conversation: the user's messages and our earlier replies.

    Returns what kind of reply it is and its markdown: a 'plan', a 'question' back to the user
    when the request is too vague to research, or a 'notice' when the research could not finish.
    """
    research_run = Research(hooks=hooks, request="\n".join(message["content"] for message in conversation if message["role"] == "user"))
    started = time.monotonic()

    # 1. Research: ask if the request is vague. Otherwise find the user, pick spots, fan out, and go
    #    back for gaps. Capped in turns and in time.
    messages = [{"role": message["role"], "content": message["content"]} for message in conversation]
    messages[-1]["content"] = f"Today is {date.today():%A, %B %d, %Y}.\n\n{messages[-1]['content']}"
    try:
        planned = await asyncio.wait_for(Runner.run(lead, messages, context=research_run, hooks=hooks, max_turns=LEAD_TURNS), RESEARCH_SECONDS)
        said = str(planned.final_output).strip()
        cut_short = ""
    except (asyncio.TimeoutError, MaxTurnsExceeded) as e:
        # Stopping the Lead also stops any researcher still out. What the others found is kept.
        limit = f"{RESEARCH_SECONDS} seconds" if isinstance(e, asyncio.TimeoutError) else f"{LEAD_TURNS} turns"
        hooks.activity("Lead", f"stopped at the limit of {limit}: {len(research_run.findings)} researcher(s) had reported")
        said, cut_short = "", limit
        if not research_run.findings:
            return "notice", f"The research hit its limit of {limit} before any researcher reported. Please try again, or ask about a smaller area."
    if not research_run.findings:
        if said and said != "DONE":
            # Nothing was researched: the Lead is asking for what it needs, or saying what it can't help with.
            return ("question" if "?" in said else "notice"), said
        return "notice", "No researcher reported anything, so there is nothing to build a plan from. Please try again."

    # 2. Write: one call, outside the loop. The Writer sees only the request and the findings.
    findings = "\n\n---\n\n".join(research_run.findings)
    gaps = ""
    if research_run.unchecked:
        gaps += f"\n\nSpots that could not be checked: {', '.join(dict.fromkeys(research_run.unchecked))}."
    if cut_short:
        gaps += f"\n\nThe research was stopped at its limit of {cut_short}, so some checks did not finish."
    flagged = ""
    for location_id, spot in research_run.checked.items():
        lines = flagged_at(location_id, hooks.tool_calls)
        if lines:
            flagged += f"\n{spot}:\n" + "\n".join(lines)
    if flagged:
        flagged = "\n\nSightings eBird flags as unusual at the spots checked:" + flagged
    brief = f"Conversation:\n{as_transcript(conversation)}\n\n{starting_point(hooks.tool_calls)}{gaps}{flagged}\n\nFindings:\n\n{findings}"
    report = str((await Runner.run(writer, brief, hooks=hooks)).final_output)

    # 3. Check: a link that no tool returned gets one rewrite, then is removed
    made_up = links_not_in_data(report, hooks.tool_calls)
    if made_up and time.monotonic() - started < REWRITE_BEFORE_SECONDS:
        hooks.activity("Citation check", f"{len(made_up)} link(s) are not in any tool result: asking the Writer to fix them")
        fix = f"{brief}\n\n---\n\nYour draft:\n\n{report}\n\n---\n\nThese links in your draft are not in the findings:\n" + "\n".join(made_up)
        fix += "\n\nWrite the plan again. Use only links that appear in the findings, copied exactly."
        report = str((await Runner.run(writer, fix, hooks=hooks)).final_output)
        made_up = links_not_in_data(report, hooks.tool_calls)
    if made_up:
        hooks.activity("Citation check", f"removed {len(made_up)} link(s) that are not in any tool result")
        report = without_links(report, made_up)
    hooks.activity("Citation check", f"all {len(set(LINK.findall(report)))} links in the plan come from tool results")
    return "plan", f"{report.rstrip()}\n\n{DATA_NOTE}"


# --- FastAPI App ---

# run_id or session_id -> every agent's context, in order. In-memory, single process. The context
# panel reads it. Only the newest runs are kept: one run's contexts can be a few hundred KB.
sessions: dict[str, AgentContexts] = {}
MAX_RUNS_KEPT = 50
# session_id -> the conversation so far: the user's messages and our replies, so an answer to a
# clarifying question continues the same request. In-memory, single process.
conversations: dict[str, list[dict]] = {}

app = FastAPI()
add_context_panel(app, sessions)


def new_contexts(key: str) -> AgentContexts:
    sessions.pop(key, None)
    sessions[key] = AgentContexts()
    while len(sessions) > MAX_RUNS_KEPT:
        sessions.pop(next(iter(sessions)))  # dicts keep insertion order: this is the oldest
    return sessions[key]


async def reply_to(session_id: str, message: str, hooks: RunLog) -> tuple[str, str]:
    """Add the message to its conversation, answer it, and remember the answer."""
    conversation = conversations.setdefault(session_id, [])
    conversation.append({"role": "user", "content": message})
    try:
        kind, reply = await deep_research(list(conversation), hooks)
    except BaseException:
        conversation.pop()  # no answer was given: don't leave the message hanging in the conversation
        raise
    conversation.append({"role": "assistant", "content": reply, "kind": kind})
    return kind, reply


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    # Every tool call by every agent, in the order they finished: {agent, name, args, result}.
    # The Lead's 'research' calls are the sub-agent runs; the Researchers' own calls follow the same shape.
    tool_calls: list[dict]


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/style.css")
def stylesheet():
    return FileResponse(HERE / "style.css", headers={"Cache-Control": "no-cache"})


@app.get("/outing.js")
def outing_script():
    """The page's map, timeline and species cards, drawn from the tool calls of a finished run."""
    return FileResponse(HERE / "outing.js", headers={"Cache-Control": "no-cache"})


# species name -> its photo, or None when there is none to show. Asked once per species.
photos: dict[str, dict | None] = {}


@app.get("/photo")
def photo(species: str):
    """A licensed photo for a species card whose bird has no photo among the run's observations."""
    if species not in photos:
        try:
            found = inaturalist.bird_photo(species)
        except LookupError:
            found = None  # the two sources name this bird differently: no photo, no guess
        except Exception:
            return {}  # iNaturalist is unreachable: don't remember that as "no photo"
        if len(photos) >= 500:
            photos.clear()
        photos[species] = found
    return photos[species] or {}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Answer one message and return it with every tool call made on the way. The same response
    shape as Project 1, with an 'agent' field on each tool call."""
    session_id = request.session_id or str(uuid.uuid4())
    hooks = RunLog(new_contexts(session_id), lambda event: None)
    try:
        _, response = await reply_to(session_id, request.message, hooks)
    except litellm.RateLimitError:
        # Still busy after the retries: tell the user plainly instead of dumping JSON.
        response = "The AI service is busy right now. Please wait a few seconds and send your message again."
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response = f"Model call failed: {type(e).__name__}: {str(e)[:300]}"
    return ChatResponse(response=response, session_id=session_id, tool_calls=hooks.tool_calls)


@app.post("/clear")
def clear(session_id: str | None = None):
    conversations.pop(session_id, None)
    sessions.pop(session_id, None)
    return {"status": "ok"}


@app.get("/run")
async def run(question: str, session_id: str | None = None):
    """The same answer as /chat, streamed as Server-Sent Events so the page can show each agent's
    steps as they happen. Send the session_id from an earlier reply to continue that conversation."""
    session_id = session_id or str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    hooks = RunLog(new_contexts(run_id), queue.put_nowait)

    async def work():
        try:
            kind, reply = await reply_to(session_id, question, hooks)
            queue.put_nowait({"type": "done", "kind": kind, "markdown": reply, "tool_calls": hooks.tool_calls})
        except Exception as e:
            # Auth, quota, a model that won't stop: show it on the page, not as a dropped stream.
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id, 'session_id': session_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    # Cloud Run sets PORT and needs the server reachable from outside the container.
    # Locally there is no PORT, so stay on 127.0.0.1:8000 (not exposed to the network).
    if "PORT" in os.environ:
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
    else:
        uvicorn.run(app, host="127.0.0.1", port=8000)
