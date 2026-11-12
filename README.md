# Bird Side Quests

"Find me something cool to see outside." Bird Side Quests is a deep research agent for **beginner birdwatchers in New York City**. You say where you are and how long you have, and it turns the last few weeks of volunteer bird sightings into a short outing plan: which nearby spot to walk to, which birds were reported there, which of them are unusual for the place and season, and how well the evidence holds up. Every fact links to the sighting record it came from.

- **Path:** Deep Research (`"path": "research"`)
- **Deployed at:** https://bird-finding-git-638434810531.europe-west1.run.app (Columbia sign-in required)
- **Author:** sm5917

## Three sample queries

Each query names a place and a fixed date, so it is answered in one turn and the answer does not drift as new sightings come in. Expected answers were checked against the sources on November 11, 2026. A count can still move by one or two if an observer adds or edits a checklist later.

"No tools" is the same model the Lead and Writer use (`vertex_ai/gemini-3.5-flash`), called once with only the query: no tools and no system prompt.

### 1. What was reported at one spot on one day

> I'm at Columbia University. Which birds were reported at Morningside Park on November 5, 2026?

| | |
|---|---|
| **Expected answer** | eBird has **40 species** at Morningside Park that day, among them Bullock's Oriole, Yellow-breasted Chat, Yellow-bellied Sapsucker, Nashville Warbler and Blue-headed Vireo. iNaturalist has one species within 0.5 km that day: Bullock's Oriole, 3 observations. |
| **Where it comes from** | eBird API `GET /v2/data/obs/L1799559/historic/2026/11/5` (tool `sightings_on_date`). iNaturalist API `GET /v1/observations/species_counts` with `lat=40.8067376&lng=-73.9580993&radius=0.5&taxon_id=3&d1=2026-11-05&d2=2026-11-05` (tool `inaturalist_sightings`). |
| **Agent** | "On November 5, 2026, eBird records show 40 bird species reported at Morningside Park. This park is located just 0.39 km from your starting point at Columbia University [...] Ten of the species you could have looked for on that day include the Bullock's Oriole, Yellow-breasted Chat, Yellow-bellied Sapsucker, Ruby-crowned Kinglet, Nashville Warbler, American Redstart, Blue-headed Vireo, Downy Woodpecker, White-throated Sparrow, and Dark-eyed Junco." It went on to mark the oriole as "both sources" and the rest as "eBird only". |
| **No tools** | "I cannot provide this information. My knowledge cutoff is January 2025, and I do not have access to real-time data or records of bird sightings for future dates such as November 5, 2026." |

### 2. Comparing two spots

> I'm at Columbia University. On November 8, 2026, which had more bird species reported: Morningside Park or the North End of Central Park?

| | |
|---|---|
| **Expected answer** | **Morningside Park, 34 species to 30.** |
| **Where it comes from** | eBird API `GET /v2/data/obs/L1799559/historic/2026/11/8` (Morningside Park) and `GET /v2/data/obs/L794140/historic/2026/11/8` (Central Park, North End). Two Researchers, one per spot, each calling `sightings_on_date`. |
| **Agent** | "On November 8, 2026, Morningside Park had more bird species reported than the North End of Central Park, with 34 species compared to 30 species. Morningside Park is 0.39 km away [...] The North End of Central Park is 1.39 km away and would take about 17 to 21 minutes to reach on foot [...]" |
| **No tools** | "I cannot answer this question because November 8, 2026, is in the future. [...] Historically, the **North End of Central Park** typically yields a higher number of reported bird species than Morningside Park." The general expectation it falls back on is the opposite of what the records show for that day. |

### 3. Is a sighting unusual, and did it happen

> I'm at Columbia University. Is November a usual month for a Bullock's Oriole around here, and was one reported at Morningside Park on November 10, 2026?

| | |
|---|---|
| **Expected answer** | **Yes, one was reported:** eBird has a Bullock's Oriole at Morningside Park on November 10, 2026 ([checklist S409614007](https://ebird.org/checklist/S409614007)), one of 24 species there that day. **It is not a usual bird here in any month:** all 10 iNaturalist observations within 25 km, over all years, are from November, with none in the other eleven months. (That count was 10 on November 11, 2026 and will grow while the bird stays.) |
| **Where it comes from** | eBird API `GET /v2/data/obs/L1799559/historic/2026/11/10` (tool `sightings_on_date`). iNaturalist API `GET /v1/observations/histogram` with `taxon_id=9357&lat=40.8067376&lng=-73.9580993&radius=25&interval=month_of_year` (tool `season_history`). |
| **Agent** | "A Bullock's Oriole was reported at Morningside Park on November 10, 2026. November appears to be the only month this species is historically seen in the area, with 10 historical November observations recorded on iNaturalist within 25 km, and zero observations in any other month." |
| **No tools** | Right in general, unable on the specifics: "No, it is not 'usual' to see a Bullock's Oriole in New York City [...] it is considered a rare vagrant [...] Because my knowledge cutoff is January 2025, I cannot verify whether a Bullock's Oriole was reported at Morningside Park on November 10, 2026." |

### Two more to try

- **A vague request, to see the clarifying question:** "Find me something cool to see outside." The agent replies with one question (where are you, how long do you have) and makes no tool calls. Answer it in the same conversation and it plans the outing.
- **A live request, the main use:** "I'm near Columbia University and have an hour. I'm a beginner. Where should I go to see something cool?" The answer changes daily. On November 11, 2026 it sent four Researchers to Morningside Park, the Columbia campus, Riverside Park Bird Sanctuary and St. John the Divine, and led with the Bullock's Oriole at Morningside Park (on 174 eBird checklists, reviewer-confirmed, and on iNaturalist).

## How it works

Three agent shapes, all in [app.py](app.py), built on the OpenAI Agents SDK with Gemini on Vertex AI.

| Agent | Job | Tools |
|---|---|---|
| **Lead** (`gemini-3.5-flash`) | Checks the request, works out where the user is, lists nearby birding spots and unusual sightings, picks spots, and sends out Researchers. | `find_birding_spots`, `unusual_sightings`, `research` |
| **Researcher** (`gemini-3.5-flash-lite`), up to 6 per request | Checks one spot against both sources in a fresh context and reports back, each fact with its source link. | `sightings_at_spot`, `inaturalist_sightings`, `season_history`, `sightings_on_date` |
| **Writer** (`gemini-3.5-flash`) | Sees only the conversation and the findings, and writes the cited plan. | none |

How each Deep Research requirement is met:

- **Fan-out in one turn.** The Lead calls `research` once per spot in a single turn. Each call clones the Researcher into a fresh context, and the SDK runs the calls concurrently. On the page the Researcher cards appear side by side.
- **Goes back for more.** After reading the findings the Lead looks for three kinds of gap: a spot with few recent records, a bird only one source has, or nothing matching what the user asked for. For each it sends another Researcher, at most twice. A request from the Staten Island Mall, where records are sparse, triggered a second round.
- **Caps.** Six Researchers per request, 12 model turns for the Lead and for each Researcher, and 150 seconds for the research phase, after which the Writer works with whatever was found. A typical answer takes 25 to 75 seconds.
- **Clarification.** If the request gives no place the agent can put on a map, or no time for an outing, the Lead calls no tools and asks one question. The server keeps the conversation per session, so the answer continues the same request. An off-topic request gets a short note on what the agent can do.
- **Accurate citations.** Every record a tool returns carries a `source` link. After the Writer finishes, code compares every link in the plan with the tool results of that run. A link that no tool returned gets one rewrite and is then removed. This proves each link is real. It cannot prove each link supports the sentence it follows.
- **Shows its work.** `POST /chat` keeps the Project 1 response shape (`response`, `session_id`, `tool_calls`), and every tool call carries an `agent` field. The Lead's `research` calls are the sub-agent runs, and each Researcher's own calls are listed under its name. The page has a "Show the work" list of the same calls and an optional panel showing the exact context each agent saw.

## The data

Two live sources, read through their public APIs. The agent uses data the model cannot know: sightings from the last few days.

| Source | What the agent reads | Access |
|---|---|---|
| **eBird API 2.0** (Cornell Lab of Ornithology), in [ebird.py](ebird.py) | Birding spots near a point, each species' latest sighting at a spot or near a point, sightings flagged as unusual for the place or date, and every species reported at a spot or in a county on a past day. | Free key from [ebird.org/api/keygen](https://ebird.org/api/keygen), set as `EBIRD_API_KEY`. |
| **iNaturalist API v1**, in [inaturalist.py](inaturalist.py) | Bird observations near a point for any period, counts per species, and one species' observations by calendar month over all years. | No key. |

To load it yourself: put an eBird key in `.env`, then run `uv run ebird.py` and `uv run inaturalist.py`. Each prints live records from around Columbia University.

Nothing is stored. There is no data file to ship with the deploy: the service needs only the eBird key. The sample queries above stay answerable because eBird's past-day lookup and iNaturalist's date ranges reach any date.

Neither dataset is on the class sign-up sheet.

## What the data can't answer

The agent says so in its answers, and code appends a fixed note about the records to every plan.

- **Whether you will see the bird.** Records describe the past. The agent says when a bird was last reported and never promises it will be there.
- **Where birds are, as opposed to where people looked.** A spot with few records may only have had few visitors.
- **How many birds there are.** A rare bird draws a crowd: one oriole produced 404 eBird rows on 174 checklists. Report counts measure attention, not population.
- **How many times a common bird was reported at a spot.** eBird's spot listing gives only each species' latest sighting. Counts of reports exist only for sightings eBird flags as unusual.
- **Anything more than 30 days back in eBird's "recent" and "unusual" listings.** Older dates work only through the past-day lookup, which has no "unusual" flag.
- **Walking time.** The agent has no street data. Distances are straight lines, and any walking time it gives is an estimate it labels as one.
- **Disagreement between the sources.** iNaturalist has far fewer records than eBird, so a bird missing from it is weaker support, not a contradiction. The plan marks each bird "both sources", "eBird only" or "iNaturalist only".
- **Where a place is.** Neither source turns a place name into coordinates reliably, so the Lead uses the coordinates it knows for well-known New York places and checks them against the nearest birding spots. For a place it does not know, it asks.

## The front end

[index.html](index.html), [style.css](style.css) and [outing.js](outing.js) replace the starter page with a field-notebook design: a landscape banner, forest greens on cream paper, and a leaf as the recurring mark.

- Three cards say what the agent explores, who it is for and how to use it, above a request box with sample requests.
- **Set up a quest on a map instead of typing.** Click where you are (or use your browser's location), pick 30 minutes, 1 hour or 2 hours, and a shaded circle shows roughly how far you could walk. The page sends the coordinates and the time as the request, so no clarifying question is needed.
- **The search plays out on a map.** As soon as the Lead knows where you are, a map appears (OpenStreetMap). The spots it could choose show as hollow dots. Each spot it sends a Researcher to becomes a pin that pulses until that Researcher reports back, so the parallel research is visible as it happens. Below the map, one line per agent shows what it is doing right now.
- **The plan arrives as a quest.** A headline ("Find the Bullock's Oriole at Morningside Park"), the Writer's opening paragraph with its sources, and a card per bird with a licensed photo, its spot and distance, and a month-by-month chart. The full cited plan opens underneath.
- **Each bird carries an evidence label** worked out in code from the records: strong lead, fresh lead, cooling trail or long shot, with the reason beside it ("reported yesterday · both sources · 174 checklists, reviewer-confirmed"). The page says plainly that a label describes the records, not the odds of seeing the bird.
- **The views are linked.** Pointing at a bird's card or its timeline row lights up its spot and its iNaturalist observations on the map. Clicking a pin shows only that spot's birds.
- **A field journal.** "Found it!" on a card stamps the bird in a journal kept in your browser, with the date, the spot and a "rare find" mark for unusual sightings. The journal is sent with your next request so the agent can look for something new.
- A timeline shows when each bird was reported by each source, with a table view of the same records.
- A clarifying question appears in its own box, and the conversation continues when you answer.
- "Show the work" lists every tool call by every agent, and an optional panel shows the exact context each agent saw.

## Run it locally

```
cp .env.example .env                    # then add your eBird key
gcloud auth application-default login   # Vertex AI uses your gcloud credentials
uv run app.py                           # http://localhost:8000
```

Or call the API directly:

```
curl -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"message": "I am at Columbia University. Which birds were reported at Morningside Park on November 5, 2026?"}'
```

The response is `{"response": "...", "session_id": "...", "tool_calls": [{"agent": "Lead", "name": "find_birding_spots", "args": {...}, "result": "..."}, ...]}`. Send the `session_id` back to continue a conversation.

## Files

| File | What it is |
|---|---|
| [app.py](app.py) | The tools, the three agents, the citation check, and the web endpoints (`/chat`, `/run`, `/clear`). |
| [ebird.py](ebird.py), [inaturalist.py](inaturalist.py) | One module per data source, each with a smoke test. |
| [index.html](index.html), [style.css](style.css), [outing.js](outing.js) | The page, its styles, and its map, quest cards, journal and timeline. |
| [context_panel.py](context_panel.py), [context-panel.js](context-panel.js) | The course's context panel, unchanged. |
| [submission.json](submission.json) | Deploy URL, author and path. |
