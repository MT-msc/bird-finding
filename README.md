# Bird Finding

This is a research agent that helps **beginner birdwatchers in New York City** find cool birds to see outside.

Simply just tell the agent where you are and how much time you have, then the agent looks at bird sightings from the last few weeks and gives you a short plan:

- which nearby spot to walk to
- which birds were seen there
- which of them are rare for that place and time of year
- how strong the evidence is

Every fact links to the sighting record it came from.

**Try it:** https://bird-finding-git-638434810531.europe-west1.run.app (Columbia sign-in required)

## Where the data comes from

Two free, public APIs, read live.

| Source | What the agent uses it for | Access |
|---|---|---|
| **eBird API 2.0** (Cornell Lab of Ornithology), in [ebird.py](ebird.py) | Birding spots near you, recent sightings, sightings flagged as unusual, and every species seen at a spot or in a county on a past day | Free key from [ebird.org/api/keygen](https://ebird.org/api/keygen), saved as `EBIRD_API_KEY` |
| **iNaturalist API v1**, in [inaturalist.py](inaturalist.py) | Bird observations near a point for any dates, counts per species, and which months a bird is usually seen | No key needed |

**To load the data yourself:** put your eBird key in `.env`, then run `uv run ebird.py` and `uv run inaturalist.py`. Each prints live sightings near Columbia University.

## Three sample queries

Each question uses a fixed date, so the answer doesn't change as new sightings come in. Answers were checked on November 11, 2026. A count may still move by one or two if someone adds or edits a checklist later.

**"No tools"** means the same model the agent uses (`vertex_ai/gemini-3.5-flash`), asked the question with no tools and no system prompt.

### 1. What was seen at one spot on one day

> I'm at Columbia University. Which birds were reported at Morningside Park on November 5, 2026?

- **Expected answer:** eBird has **40 species** at Morningside Park that day, including Bullock's Oriole, Yellow-breasted Chat, Yellow-bellied Sapsucker, Nashville Warbler and Blue-headed Vireo. iNaturalist has one species within 0.5 km that day: Bullock's Oriole, with 3 observations.
- **Where it comes from:**
  - eBird: `GET /v2/data/obs/L1799559/historic/2026/11/5`
  - iNaturalist: `GET /v1/observations/species_counts?lat=40.8067376&lng=-73.9580993&radius=0.5&taxon_id=3&d1=2026-11-05&d2=2026-11-05`
- **Our agent:** "On November 5, 2026, eBird records show 40 bird species reported at Morningside Park. This park is located just 0.39 km from your starting point at Columbia University [...]" It then named ten of the species, marked the oriole "both sources" and the rest "eBird only".
- **No tools:** "I cannot provide this information. My knowledge cutoff is January 2025, and I do not have access to real-time data or records of bird sightings for future dates such as November 5, 2026."

### 2. Comparing two spots

> I'm at Columbia University. On November 8, 2026, which had more bird species reported: Morningside Park or the North End of Central Park?

- **Expected answer:** **Morningside Park, 34 species to 30.**
- **Where it comes from:**
  - Morningside Park: eBird `GET /v2/data/obs/L1799559/historic/2026/11/8`
  - Central Park, North End: eBird `GET /v2/data/obs/L794140/historic/2026/11/8`
- **Our agent:** "On November 8, 2026, Morningside Park had more bird species reported than the North End of Central Park, with 34 species compared to 30 species. Morningside Park is 0.39 km away [...] The North End of Central Park is 1.39 km away and would take about 17 to 21 minutes to reach on foot [...]"
- **No tools:** "I cannot answer this question because November 8, 2026, is in the future. [...] Historically, the **North End of Central Park** typically yields a higher number of reported bird species than Morningside Park." Its guess is the opposite of what the records show for that day.

### 3. Is a sighting unusual, and did it happen?

> I'm at Columbia University. Is November a usual month for a Bullock's Oriole around here, and was one reported at Morningside Park on November 10, 2026?

- **Expected answer:**
  - **Yes, one was reported.** eBird has a Bullock's Oriole at Morningside Park on November 10, 2026 ([checklist S409614007](https://ebird.org/checklist/S409614007)), one of 24 species there that day.
  - **It is not a usual bird here in any month.** All 10 iNaturalist observations within 25 km, over all years, are from November. None are from the other eleven months. (The count was 10 on November 11, 2026 and will grow while the bird stays.)
- **Where it comes from:**
  - eBird: `GET /v2/data/obs/L1799559/historic/2026/11/10`
  - iNaturalist: `GET /v1/observations/histogram?taxon_id=9357&lat=40.8067376&lng=-73.9580993&radius=25&interval=month_of_year`
- **Our agent:** "A Bullock's Oriole was reported at Morningside Park on November 10, 2026. November appears to be the only month this species is historically seen in the area, with 10 historical November observations recorded on iNaturalist within 25 km, and zero observations in any other month."
- **No tools:** right in general, but can't check the specific day: "No, it is not 'usual' to see a Bullock's Oriole in New York City [...] it is considered a rare vagrant [...] Because my knowledge cutoff is January 2025, I cannot verify whether a Bullock's Oriole was reported at Morningside Park on November 10, 2026."

## Project structure

| File | What it does |
|---|---|
| [app.py](app.py) | The main app: the tools, the three agents (Lead, Researchers, Writer), the citation check, and the web endpoints (`/chat`, `/run`, `/clear`) |
| [ebird.py](ebird.py) | Reads the eBird API |
| [inaturalist.py](inaturalist.py) | Reads the iNaturalist API |
| [index.html](index.html), [style.css](style.css), [outing.js](outing.js) | The web page: its layout, styles, map, bird cards, journal and timeline |
| [context_panel.py](context_panel.py), [context-panel.js](context-panel.js) | The course's context panel, unchanged |
| [pyproject.toml](pyproject.toml), [uv.lock](uv.lock) | Python dependencies |
| [submission.json](submission.json) | Deploy URL, author and path |
