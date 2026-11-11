// Bird Side Quests: the map, the sightings timeline and the species cards.
//
// Everything is drawn from the tool calls of one finished run, the same list /chat returns, so
// the pictures show the records the plan was written from and nothing else.
//   readRun(toolCalls)  -> the run as data: start, spots checked, birds worth looking for
//   createStage()       -> the map the research plays out on, live
//   questCard(run)      -> the plan as a quest: a headline, and a card per bird with how well the records back it
//   drawTimeline(run)   -> when each bird was reported, by source
//   linkViews(stage, quest, timeline) -> pointing at a bird lights it up on the map; a pin filters the cards
//   createPicker(box, onChange)       -> a small map to click where you are, with a ring for how far you might walk
//   The field journal, kept in this browser: journalEntries(), seenBirds(), forgetBird(key), clearSeen()
//
// Bird names, place names and photo credits are text that volunteers typed into eBird and
// iNaturalist. They only ever go into the page through textContent, never as HTML.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MAX_BIRDS = 8; // cards and timeline rows: past this the page is a list, not a plan
const DAY_MS = 86_400_000;

// --- Small helpers ---

function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
}

function link(text, url) {
    const a = node("a", "", text);
    // Only web links: a record's "source" is data, so it is not trusted to be one
    if (/^https?:\/\//.test(url ?? "")) {
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
    }
    return a;
}

// "2026-11-10 13:15" or "2026-11-10" -> the day, as a UTC timestamp. Only the date part is read,
// so a browser in another time zone can't shift a sighting to the day before.
function dayOf(text) {
    const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text ?? "");
    return match ? Date.UTC(+match[1], +match[2] - 1, +match[3]) : null;
}

function shortDate(day) {
    const date = new Date(day);
    return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}`;
}

function parseResult(call) {
    try {
        const result = JSON.parse(call.result);
        return result && !result.error ? result : null;
    } catch {
        return null; // a researcher's findings are prose, not JSON
    }
}

// A spot's name, short enough for a label:
//   "St. John the Divine (Amsterdam Ave-Morningside Dr; 110-113th St)" -> "St. John the Divine"
//   "Central Park--The Lake" -> "The Lake", or in full, "The Lake, Central Park"
// eBird names a spot inside a park "Park--Part". Cut short, four such names all read "Central Park--…".
function shortPlace(name, full = false) {
    let place = (name ?? "").split(" (")[0].replace(/, (Manhattan|Queens|Brooklyn)$/, "");
    const [park, part] = place.split("--");
    if (part) place = full ? `${part}, ${park}` : part;
    return !full && place.length > 34 ? place.slice(0, 32) + "…" : place;
}

const sameBird = (record, bird) =>
    (record.scientific_name ?? "").toLowerCase() === bird.scientific.toLowerCase() || (record.species ?? "").toLowerCase() === bird.species.toLowerCase();

// --- Read the run: where the visitor started, the spots that were checked, the birds worth a card ---

function readRun(toolCalls) {
    const run = { start: null, spots: [], birds: [], days: [] };

    const spotList = [...toolCalls].reverse().find((call) => call.name === "find_birding_spots" && parseResult(call)?.spots?.length);
    if (spotList) run.start = { lat: spotList.args.lat, lng: spotList.args.lng };

    const flaggedCall = [...toolCalls].reverse().find((call) => call.name === "unusual_sightings" && parseResult(call));
    const flagged = flaggedCall ? (parseResult(flaggedCall).entries ?? []) : [];

    // One spot per researcher that reported. A refused or failed researcher left no findings.
    for (const call of toolCalls) {
        if (call.name !== "research" || /^(No researchers left|The researcher for)/.test(call.result)) continue;
        if (run.spots.some((spot) => spot.id === call.args.location_id)) continue;
        run.spots.push({
            name: call.args.spot,
            id: call.args.location_id,
            lat: call.args.lat,
            lng: call.args.lng,
            distanceKm: call.args.distance_km,
            flagged: flagged.filter((entry) => entry.location_id === call.args.location_id),
            ebird: null,
            inat: null,
            histories: [],
        });
    }

    // Give each spot its researcher's results. A researcher is matched to its spot by the
    // location it asked eBird about, or failing that by the point it asked iNaturalist about.
    const byAgent = {};
    for (const call of toolCalls) {
        if (call.agent.startsWith("Researcher")) (byAgent[call.agent] ??= []).push(call);
    }
    for (const calls of Object.values(byAgent)) {
        // For a question about one past day the eBird list comes from sightings_on_date instead
        const atSpot = calls.find((call) => call.name === "sightings_at_spot" && parseResult(call)) ?? calls.find((call) => call.name === "sightings_on_date" && parseResult(call));
        const nearby = calls.find((call) => call.name === "inaturalist_sightings" && parseResult(call));
        const spot =
            run.spots.find((s) => s.id === (atSpot?.args.location_id ?? atSpot?.args.place)) ??
            run.spots.find((s) => nearby && Math.abs(s.lat - nearby.args.lat) < 0.001 && Math.abs(s.lng - nearby.args.lng) < 0.001);
        if (!spot) continue;
        spot.ebird = atSpot ? parseResult(atSpot) : null;
        spot.inat = nearby ? parseResult(nearby) : null;
        spot.histories = calls.filter((call) => call.name === "season_history").map(parseResult).filter(Boolean);
    }

    // The days the records cover: iNaturalist's window when a researcher asked for one, else the last 14
    const window = run.spots.find((spot) => spot.inat?.window)?.inat.window;
    const last = dayOf(window?.d2) ?? dayOf(new Date().toISOString());
    const first = dayOf(window?.d1) ?? last - 14 * DAY_MS;
    for (let day = first; day <= last; day += DAY_MS) run.days.push(day);

    // The birds: every sighting eBird flags as unusual at a checked spot, then the ones a
    // researcher looked up the season of. One entry per species, at the spot with the most reports.
    const birds = new Map();
    const bird = (scientific, species, spot) => {
        const key = (scientific || species).toLowerCase();
        if (!birds.has(key)) birds.set(key, { species, scientific: scientific ?? "", spot, flag: null, months: null });
        return birds.get(key);
    };
    for (const spot of run.spots) {
        for (const entry of spot.flagged) {
            const found = bird(entry.scientific_name, entry.species, spot);
            if (!found.flag || entry.checklists > found.flag.checklists) {
                found.flag = entry;
                found.spot = spot;
            }
        }
    }
    for (const spot of run.spots) {
        for (const history of spot.histories) {
            const found = bird(history.scientific_name, history.species, spot);
            found.months = history.observations_by_month;
            found.historySource = history.source;
        }
    }

    for (const found of birds.values()) {
        const spot = found.spot;
        const latest = spot.ebird?.sightings?.find((sighting) => sameBird(sighting, found));
        found.ebird = found.flag
            ? { day: dayOf(found.flag.seen_at), firstDay: dayOf(found.flag.first_seen_at), source: found.flag.source, checklists: found.flag.checklists, confirmed: found.flag.confirmed_by_reviewer }
            : latest
              ? { day: dayOf(latest.seen_at), source: latest.source }
              : null;
        const counted = spot.inat?.species?.find((species) => sameBird(species, found));
        const observations = (spot.inat?.observations ?? []).filter((seen) => sameBird(seen, found) && dayOf(seen.seen_on) !== null);
        found.inat = counted || observations.length ? { count: counted?.observations ?? observations.length, source: counted?.source, observations } : null;
        const pictured = observations.find((seen) => seen.photo_url);
        found.photo = pictured ? { url: pictured.photo_url, credit: pictured.photo_credit, source: pictured.source } : null;
        found.sources = found.ebird && found.inat ? "Both sources" : found.ebird ? "eBird only" : found.inat ? "iNaturalist only" : "";
    }

    // Best attested first: most checklists, then birds both sources have, then the most recent
    const byEvidence = (a, b) =>
        (b.flag?.checklists ?? 0) - (a.flag?.checklists ?? 0) || (b.sources === "Both sources") - (a.sources === "Both sources") || (b.ebird?.day ?? 0) - (a.ebird?.day ?? 0);
    const all = [...birds.values()].filter((found) => found.ebird || found.inat);
    const unusual = all.filter((found) => found.flag).sort(byEvidence);
    const lookedUp = all.filter((found) => !found.flag && found.months).sort(byEvidence);
    // Unusual sightings get half the places at most, so one busy park can't take every card and
    // crowd out the everyday birds the researchers looked into at the other spots
    const half = MAX_BIRDS / 2;
    run.birds = [...unusual.slice(0, half), ...lookedUp, ...unusual.slice(half)].slice(0, MAX_BIRDS);
    // A question about one past day ("what was reported on Nov 5") rather than an outing
    const lists = run.spots.map((spot) => spot.ebird).filter(Boolean);
    run.date = lists.length && lists.every((list) => list.date) ? lists[0].date : null;
    for (const found of run.birds) found.evidence = evidenceFor(found, run);
    return run;
}

// --- How well the records back a bird up ---
//
// Four steps, worked out from the records alone, so the same sighting always gets the same label.
// It describes the evidence. It is not the chance of seeing the bird, and the page says so.
//   3 Strong lead    reported in the last 3 days, and either both sources have it or 5+ reviewer-confirmed checklists do
//   2 Fresh lead     reported in the last 3 days
//   1 Cooling trail  last reported 4 to 7 days ago
//   0 Long shot      nothing for over a week, a flagged sighting no reviewer has confirmed on 1 or 2
//                    checklists, or a bird only iNaturalist has

const EVIDENCE = ["Long shot", "Cooling trail", "Fresh lead", "Strong lead"];

function evidenceFor(bird, run) {
    const facts = [];
    if (bird.sources) facts.push(bird.sources === "Both sources" ? "both sources" : bird.sources);
    if (bird.ebird?.checklists) facts.push(`${bird.ebird.checklists} checklist${bird.ebird.checklists === 1 ? "" : "s"}, ${bird.ebird.confirmed ? "reviewer-confirmed" : "not yet confirmed"}`);
    // For a question about one past day there is no "how recent": the bird was reported that day or it wasn't
    if (run.date) return { level: null, label: "Reported that day", why: facts.join(" · ") };

    const today = run.days[run.days.length - 1];
    const last = Math.max(bird.ebird?.day ?? 0, ...(bird.inat?.observations ?? []).map((seen) => dayOf(seen.seen_on) ?? 0));
    const age = Math.max(0, Math.round((today - last) / DAY_MS));
    const when = age === 0 ? "reported today" : age === 1 ? "reported yesterday" : `last reported ${age} days ago`;
    const wellBacked = bird.sources === "Both sources" || (bird.ebird?.confirmed && bird.ebird.checklists >= 5);
    const shaky = bird.sources === "iNaturalist only" || (bird.flag && !bird.ebird?.confirmed && (bird.ebird?.checklists ?? 0) <= 2);
    const level = age > 7 || shaky ? 0 : age > 3 ? 1 : wellBacked ? 3 : 2;
    return { level, label: EVIDENCE[level], why: [when, ...facts].join(" · ") };
}

// Three dots, as many filled as the level. The label is always written beside them.
function evidenceMeter(evidence) {
    const box = node("div", "evidence");
    if (evidence.level !== null) {
        const dots = box.appendChild(node("span", "evidence-dots"));
        dots.setAttribute("aria-hidden", "true");
        for (let i = 1; i <= 3; i++) dots.appendChild(node("span", "evidence-dot" + (i <= evidence.level ? " on" : "")));
    }
    box.appendChild(node("strong", "", evidence.label));
    if (evidence.why) box.appendChild(node("span", "evidence-why", evidence.why));
    return box;
}

// --- The stage: the map the research plays out on ---
//
// It is on the page from the moment the Lead knows where the visitor is. Spots the Lead could
// choose appear as faint dots; each one it sends a researcher to becomes a pin that pulses until
// that researcher reports back. When the run ends, the pins gain what was found there.
//   const stage = createStage()
//   stage.setStart(lat, lng, nearbySpots) · stage.addSpot(spot) · stage.report(locationId, ok) · stage.finish(run)

function createStage() {
    const section = node("figure", "viz stage");
    const title = section.appendChild(node("div", "viz-title", "The search"));
    const sub = section.appendChild(node("div", "viz-sub", "Each pin is a spot a researcher is checking. It pulses until that researcher reports back."));

    const legend = section.appendChild(node("div", "legend"));
    const key = (shape, text) => {
        const item = legend.appendChild(node("span", "key"));
        item.appendChild(node("span", "shape " + shape));
        item.appendChild(node("span", "", text));
        return item;
    };
    key("start", "You");
    key("ebird", "Spot checked (eBird)");
    key("candidate", "Other spots nearby");
    const inatKey = key("inat", "iNaturalist observation");
    inatKey.hidden = true; // until there are some on the map

    const box = section.appendChild(node("div", "map"));
    box.setAttribute("role", "application");
    box.setAttribute("aria-label", "Map of the birding spots being checked near your starting point");
    const note = section.appendChild(node("div", "viz-note", "Distances in the plan are straight lines from your starting point, not walking routes."));

    let map = null;
    let start = null;
    const waiting = []; // what to draw once the map exists
    const candidates = new Map(); // location_id -> its faint dot
    const pins = new Map(); // location_id -> {marker, line, spot}
    const observed = new Map(); // a bird's name, lower case -> the markers of its iNaturalist observations
    const names = []; // without a map: the same facts as a list
    let picked = () => {}; // told when the visitor picks a pin, or the empty map (null)

    const icon = (shape) => L.divIcon({ className: "", html: `<span class="shape ${shape} on-map"></span>`, iconSize: [24, 24], iconAnchor: [12, 12] });
    const label = (text) => node("span", "", text);
    const draw = (step) => (map ? step() : waiting.push(step));
    const frame = () => {
        const points = [start, ...[...pins.values()].map((pin) => [pin.spot.lat, pin.spot.lng])].filter(Boolean);
        if (points.length) map.fitBounds(L.latLngBounds(points).pad(0.3), { maxZoom: 16 });
    };

    if (window.L) {
        // Leaflet measures its box when it starts, so it waits until the box is on the page
        const begin = () => {
            if (!box.isConnected) return void requestAnimationFrame(begin);
            map = L.map(box, { scrollWheelZoom: false }); // the page scrolls past the map, not into it
            L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            }).addTo(map);
            map.setView([40.7831, -73.9712], 12); // Manhattan, until the Lead says where the visitor is
            map.on("click", () => picked(null));
            for (const step of waiting.splice(0)) step();
        };
        requestAnimationFrame(begin);
    } else {
        // The map library didn't load (offline, or blocked): the spots as a list instead
        box.remove();
        legend.remove();
        section.insertBefore(node("ul", "map-fallback"), note);
    }

    return {
        el: section,

        // Call fn(locationId, name) when the visitor picks a pin, and fn(null) when they click the empty map
        onPick(fn) {
            picked = fn;
        },

        // Light up one spot's pin and one bird's observations, and quiet everything else. No argument: back to normal.
        highlight(target) {
            if (!map) return;
            for (const lit of section.querySelectorAll(".shape.lit")) lit.classList.remove("lit");
            section.classList.toggle("focusing", Boolean(target));
            if (!target) return;
            const markers = [pins.get(target.spotId)?.marker, ...target.names.flatMap((name) => observed.get(name.toLowerCase()) ?? [])];
            for (const marker of new Set(markers)) marker?.getElement()?.querySelector(".shape")?.classList.add("lit");
        },

        setStart(lat, lng, nearby = []) {
            if (!window.L) return;
            draw(() => {
                if (start) return; // the Lead corrected its coordinates: keep the first drawing simple
                start = [lat, lng];
                L.marker(start, { icon: icon("start"), title: "Your starting point", zIndexOffset: 1000, keyboard: false })
                    .addTo(map)
                    .bindTooltip(label("You"), { permanent: true, direction: "bottom", offset: [0, 8], className: "map-label" });
                for (const spot of nearby) {
                    if (pins.has(spot.location_id)) continue;
                    candidates.set(spot.location_id, L.marker([spot.lat, spot.lng], { icon: icon("candidate"), title: spot.spot, keyboard: false, interactive: false }).addTo(map));
                }
                // Close in on the nearest few: the Lead picks among those
                const near = nearby.slice(0, 8).map((spot) => [spot.lat, spot.lng]);
                map.fitBounds(L.latLngBounds([start, ...near]).pad(0.25), { maxZoom: 16 });
            });
        },

        addSpot(spot) {
            if (!window.L) {
                if (!names.includes(spot.spot)) {
                    names.push(spot.spot);
                    section.querySelector(".map-fallback").appendChild(node("li", "", `${spot.spot}: ${spot.distance_km} km in a straight line`));
                }
                return;
            }
            draw(() => {
                if (pins.has(spot.location_id)) return;
                candidates.get(spot.location_id)?.remove();
                const line = start ? L.polyline([start, [spot.lat, spot.lng]], { color: "#2a78d6", weight: 2, opacity: 0.55, interactive: false }).addTo(map) : null;
                const marker = L.marker([spot.lat, spot.lng], { icon: icon("ebird working"), title: spot.spot, zIndexOffset: 500 })
                    .addTo(map)
                    .bindTooltip(label(shortPlace(spot.spot)), { permanent: true, direction: "top", offset: [0, -8], className: "map-label" });
                marker.on("click", () => picked(spot.location_id, spot.spot));
                pins.set(spot.location_id, { marker, line, spot });
                frame();
            });
        },

        // ok: the researcher reported. Not ok: it ran out of turns or failed, and the spot is unchecked.
        report(locationId, ok) {
            if (!window.L) return;
            draw(() => {
                const pin = pins.get(locationId);
                if (!pin) return;
                pin.marker.setIcon(icon(ok ? "ebird" : "unchecked"));
                pin.line?.setStyle({ opacity: ok ? 0.3 : 0.12 });
            });
        },

        // The run is over: make sure every checked spot is drawn, then add what was found there
        finish(run) {
            title.textContent = "Where to go";
            sub.textContent = "The spots the researchers checked. Tap a pin for what was reported there.";
            if (run.start) this.setStart(run.start.lat, run.start.lng);
            for (const spot of run.spots) {
                this.addSpot({ spot: spot.name, location_id: spot.id, lat: spot.lat, lng: spot.lng, distance_km: spot.distanceKm });
                this.report(spot.id, true);
            }
            if (!window.L) return;
            draw(() => {
                // A pin still pulsing belongs to a researcher the run stopped before it reported
                for (const [id, pin] of pins) {
                    if (!run.spots.some((spot) => spot.id === id)) pin.marker.setIcon(icon("unchecked"));
                }

                // iNaturalist's observations, once each: two spots' circles can hold the same one
                const drawn = new Set();
                for (const spot of run.spots) {
                    for (const seen of spot.inat?.observations ?? []) {
                        if (seen.lat == null || drawn.has(seen.source)) continue;
                        drawn.add(seen.source);
                        const popup = node("div", "map-popup");
                        popup.appendChild(node("strong", "", seen.species ?? "A bird"));
                        popup.appendChild(node("div", "", `Observed ${shortDate(dayOf(seen.seen_on))} · ${seen.quality_grade === "research" ? "identification agreed" : "identification not yet agreed"}`));
                        if (seen.photo_url) {
                            const img = popup.appendChild(node("img"));
                            img.src = seen.photo_url;
                            img.alt = `${seen.species}, photographed by an iNaturalist observer`;
                            img.loading = "lazy";
                            popup.appendChild(node("div", "credit", seen.photo_credit));
                        }
                        popup.appendChild(link("iNaturalist observation", seen.source));
                        const marker = L.marker([seen.lat, seen.lng], { icon: icon("inat"), keyboard: false, title: seen.species ?? "" }).addTo(map).bindPopup(popup);
                        // Kept under both names: the two sources don't always agree on the scientific one
                        for (const name of [seen.scientific_name, seen.species]) {
                            if (!name) continue;
                            if (!observed.has(name.toLowerCase())) observed.set(name.toLowerCase(), []);
                            observed.get(name.toLowerCase()).push(marker);
                        }
                    }
                }
                inatKey.hidden = !drawn.size;

                for (const spot of run.spots) {
                    const popup = node("div", "map-popup");
                    popup.appendChild(node("strong", "", spot.name));
                    popup.appendChild(node("div", "", `${spot.distanceKm} km from you in a straight line`));
                    if (spot.ebird) popup.appendChild(node("div", "", `${spot.ebird.species_count} species on eBird ${spot.ebird.date ? "on " + shortDate(dayOf(spot.ebird.date)) : `in the last ${spot.ebird.days_back} days`}`));
                    if (spot.inat) popup.appendChild(node("div", "", `${spot.inat.species_count} species on iNaturalist within ${spot.inat.radius_km} km`));
                    if (spot.flagged.length) {
                        popup.appendChild(node("div", "popup-head", "Flagged as unusual by eBird"));
                        const list = popup.appendChild(node("ul"));
                        for (const entry of spot.flagged.slice(0, 6)) list.appendChild(node("li", "", `${entry.species}, last ${shortDate(dayOf(entry.seen_at))}, ${entry.checklists} checklist${entry.checklists === 1 ? "" : "s"}`));
                    }
                    popup.appendChild(link("eBird spot", `https://ebird.org/hotspot/${encodeURIComponent(spot.id)}`));
                    pins.get(spot.id)?.marker.bindPopup(popup);
                }
                frame();
            });
        },
    };
}

// --- The timeline: when each bird was reported, by source ---

let tooltip = null;

function showTooltip(mark, lines) {
    tooltip ??= document.body.appendChild(node("div", "viz-tooltip"));
    tooltip.replaceChildren();
    for (const [className, text] of lines) tooltip.appendChild(node("div", className, text));
    tooltip.hidden = false;
    // Above the mark, kept inside the window
    const at = mark.getBoundingClientRect();
    const width = tooltip.offsetWidth;
    const left = Math.min(Math.max(8, at.left + at.width / 2 - width / 2), window.innerWidth - width - 8);
    tooltip.style.left = `${left + window.scrollX}px`;
    tooltip.style.top = `${at.top + window.scrollY - tooltip.offsetHeight - 6}px`;
}

function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
}

function drawTimeline(run) {
    const rows = run.birds.filter((bird) => bird.ebird?.day || bird.inat?.observations.length);
    if (!rows.length || run.days.length < 2) return null;
    const first = run.days[0];
    const count = run.days.length;
    // A day's marks sit in the middle of its column. A day outside the window is drawn at its edge.
    const x = (day) => ((Math.min(Math.max((day - first) / DAY_MS, 0), count - 1) + 0.5) / count) * 100;

    const section = node("figure", "viz");
    section.appendChild(node("div", "viz-title", "When these birds were reported"));
    section.appendChild(node("div", "viz-sub", `${shortDate(first)} to ${shortDate(run.days[count - 1])}. eBird shows only each bird's latest report at the spot, so an empty day is not a day without the bird.`));

    const legend = section.appendChild(node("div", "legend"));
    for (const [shape, text] of [
        ["ebird", "eBird: latest report"],
        ["inat", "iNaturalist: observation"],
        ["span", "eBird: first to latest report of an unusual sighting"],
    ]) {
        const key = legend.appendChild(node("span", "key"));
        key.appendChild(node("span", "shape " + shape));
        key.appendChild(node("span", "", text));
    }

    const grid = section.appendChild(node("div", "tl"));
    grid.style.setProperty("--days", count);

    const mark = (track, source, day, lane, lines, url) => {
        const button = node("button", `tl-mark ${lane}`);
        button.type = "button";
        button.style.left = `${x(day)}%`;
        button.appendChild(node("span", "shape " + source));
        button.setAttribute("aria-label", lines.map(([, text]) => text).join(". "));
        // The same details on hover and on keyboard focus; a click opens the record itself
        const show = () => showTooltip(button, lines);
        button.addEventListener("pointerenter", show);
        button.addEventListener("focus", show);
        button.addEventListener("pointerleave", hideTooltip);
        button.addEventListener("blur", hideTooltip);
        if (/^https?:\/\//.test(url ?? "")) button.addEventListener("click", () => window.open(url, "_blank", "noopener"));
        track.appendChild(button);
    };

    for (const bird of rows) {
        const row = grid.appendChild(node("div", "tl-row"));
        row.dataset.bird = birdKey(bird).toLowerCase();
        row.dataset.spot = bird.spot.id;
        const name = row.appendChild(node("div", "tl-name"));
        name.appendChild(node("span", "", bird.species));
        name.appendChild(node("small", "", shortPlace(bird.spot.name)));
        const track = row.appendChild(node("div", "tl-track"));

        if (bird.ebird?.day) {
            const where = `eBird, at ${shortPlace(bird.spot.name)}`;
            if (bird.ebird.firstDay && bird.ebird.firstDay < bird.ebird.day) {
                const span = track.appendChild(node("span", "tl-span"));
                span.style.left = `${x(bird.ebird.firstDay)}%`;
                span.style.width = `${x(bird.ebird.day) - x(bird.ebird.firstDay)}%`;
                mark(track, "ebird small", bird.ebird.firstDay, "upper", [["when", shortDate(bird.ebird.firstDay)], ["what", `First report in this period · ${where}`]], bird.ebird.source);
            }
            const lines = [["when", shortDate(bird.ebird.day)], ["what", `Latest report · ${where}`]];
            if (bird.ebird.checklists) lines.push(["what", `On ${bird.ebird.checklists} checklist${bird.ebird.checklists === 1 ? "" : "s"}, ${bird.ebird.confirmed ? "confirmed by a reviewer" : "not yet confirmed by a reviewer"}`]);
            mark(track, "ebird", bird.ebird.day, "upper", lines, bird.ebird.source);
        }

        // Several observations on one day share one mark: stacked, they would hide each other
        const byDay = new Map();
        for (const seen of bird.inat?.observations ?? []) {
            const day = dayOf(seen.seen_on);
            if (!byDay.has(day)) byDay.set(day, []);
            byDay.get(day).push(seen);
        }
        for (const [day, seen] of byDay) {
            mark(track, "inat", day, "lower", [["when", shortDate(day)], ["what", `${seen.length} observation${seen.length === 1 ? "" : "s"} · iNaturalist, near ${shortPlace(bird.spot.name)}`]], seen[0].source);
        }
    }

    // The day axis: every other day, counted back from the last. On a phone only every fourth is
    // shown, so each label carries its month: any of them may be the first one left.
    const axis = grid.appendChild(node("div", "tl-row tl-axis"));
    axis.appendChild(node("div", "tl-name"));
    const ticks = axis.appendChild(node("div", "tl-track"));
    run.days.forEach((day, index) => {
        if ((count - 1 - index) % 2) return;
        const tick = ticks.appendChild(node("span", "tl-tick" + ((count - 1 - index) % 4 ? " minor" : ""), shortDate(day)));
        tick.style.left = `${x(day)}%`;
    });

    // The same records as a table: nothing in the chart is readable only by hovering
    const details = section.appendChild(node("details", "table-view"));
    details.appendChild(node("summary", "", "View as a table"));
    const table = details.appendChild(node("table"));
    const head = table.appendChild(node("tr"));
    for (const title of ["Bird", "Spot", "eBird: latest report", "eBird: unusual sighting", "iNaturalist: observations"]) head.appendChild(node("th", "", title));
    for (const bird of rows) {
        const row = table.appendChild(node("tr"));
        row.appendChild(node("td", "", bird.species));
        row.appendChild(node("td", "", shortPlace(bird.spot.name)));
        const latest = row.appendChild(node("td"));
        if (bird.ebird?.day) latest.appendChild(link(shortDate(bird.ebird.day), bird.ebird.source));
        else latest.textContent = "none";
        row.appendChild(
            node("td", "", bird.ebird?.checklists ? `first ${shortDate(bird.ebird.firstDay)}, ${bird.ebird.checklists} checklists, ${bird.ebird.confirmed ? "confirmed" : "not yet confirmed"}` : "not flagged"),
        );
        const seen = row.appendChild(node("td"));
        const dates = [...new Set((bird.inat?.observations ?? []).map((s) => shortDate(dayOf(s.seen_on))))];
        if (bird.inat) seen.appendChild(link(`${bird.inat.count}${dates.length ? ` (${dates.join(", ")})` : ""}`, bird.inat.source));
        else seen.textContent = "none";
    }
    return section;
}

// --- The species cards, and the visitor's collection ---

const SEEN_KEY = "bird-side-quests-seen";

// The field journal: {key: {name, found, rare, spot, photo, credit}}, kept in this browser only.
// A browser that blocks storage (private mode, say) gets a journal that lasts until the page closes.
let seen = {};
try {
    seen = JSON.parse(localStorage.getItem(SEEN_KEY)) ?? {};
    // An earlier version kept only each bird's name
    for (const [key, entry] of Object.entries(seen)) if (typeof entry === "string") seen[key] = { name: entry };
} catch {
    seen = {};
}

function saveSeen() {
    try {
        localStorage.setItem(SEEN_KEY, JSON.stringify(seen));
    } catch {}
    document.dispatchEvent(new CustomEvent("collection-changed"));
}

const birdKey = (bird) => bird.scientific || bird.species;
const seenBirds = () => Object.values(seen).map((entry) => entry.name);
const journalEntries = () => Object.entries(seen).map(([key, entry]) => ({ key, ...entry }));

function setSeen(bird, isSeen) {
    if (isSeen) {
        const today = new Date();
        const found = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
        seen[birdKey(bird)] = { name: bird.species, found, rare: Boolean(bird.flag), spot: shortPlace(bird.spot.name, true), photo: bird.shown?.url, credit: bird.shown?.credit };
    } else delete seen[birdKey(bird)];
    saveSeen();
}

function forgetBird(key) {
    delete seen[key];
    saveSeen();
}

function clearSeen() {
    seen = {};
    try {
        localStorage.removeItem(SEEN_KEY);
    } catch {}
    document.dispatchEvent(new CustomEvent("collection-changed"));
}

function seasonChart(bird) {
    const counts = MONTHS.map((_, index) => bird.months[index + 1] ?? bird.months[String(index + 1)] ?? 0);
    const total = counts.reduce((sum, value) => sum + value, 0);
    const box = node("div", "season");
    if (!total) {
        box.appendChild(node("div", "season-note", "iNaturalist has no observations of it within 25 km in any month."));
        return box;
    }
    const max = Math.max(...counts);
    const now = new Date().getMonth();
    const bars = box.appendChild(node("div", "season-bars"));
    // Every value is in this label and in each bar's title: the bars are a picture of it
    bars.setAttribute("role", "img");
    bars.setAttribute("aria-label", "iNaturalist observations by month within 25 km, all years: " + counts.map((value, index) => `${MONTHS[index]} ${value}`).join(", "));
    counts.forEach((value, index) => {
        const column = bars.appendChild(node("div", "season-col"));
        column.title = `${MONTHS[index]}: ${value} observation${value === 1 ? "" : "s"}`;
        const bar = column.appendChild(node("div", "season-bar" + (index === now ? " now" : "")));
        // A month with any observations keeps a visible sliver; an empty month draws nothing
        bar.style.height = value ? `${Math.max(6, (value / max) * 100)}%` : "0";
        column.appendChild(node("span", "season-month", MONTHS[index][0]));
    });
    const peak = counts.indexOf(max);
    box.appendChild(node("div", "season-note", `By month on iNaturalist, within 25 km, all years. This month: ${counts[now]}. Most: ${MONTHS[peak]} (${max}).`));
    return box;
}

function birdCard(bird) {
    const card = node("article", "bird-card");
    const key = birdKey(bird);
    card.dataset.bird = key.toLowerCase();
    card.dataset.spot = bird.spot.id;

    const frame = card.appendChild(node("div", "bird-photo"));
    const credit = node("div", "credit");
    const showPhoto = (photo) => {
        const img = node("img");
        img.src = photo.url;
        img.alt = `${bird.species}, photo from iNaturalist`;
        img.loading = "lazy";
        frame.replaceChildren(img);
        credit.textContent = photo.credit ? `Photo: ${photo.credit}` : "";
        // The journal's stamp uses the same photo. One that arrives after "Found it!" is added then.
        bird.shown = photo;
        if (seen[key] && !seen[key].photo) {
            Object.assign(seen[key], { photo: photo.url, credit: photo.credit });
            saveSeen();
        }
    };
    // No photo among this run's observations: a plain tile now, the species' own photo if it has a licensed one
    frame.appendChild(node("span", "bird-initials", bird.species.split(/\s+/).map((word) => word[0]).join("").slice(0, 3)));
    if (bird.photo) showPhoto(bird.photo);
    else {
        fetch("/photo?species=" + encodeURIComponent(bird.scientific || bird.species))
            .then((response) => response.json())
            .then((photo) => photo.photo_url && showPhoto({ url: photo.photo_url, credit: photo.photo_credit }))
            .catch(() => {});
    }

    const body = card.appendChild(node("div", "bird-body"));
    body.appendChild(node("h4", "", bird.species));
    if (bird.scientific) body.appendChild(node("div", "bird-sci", bird.scientific));

    const chips = body.appendChild(node("div", "chips"));
    if (bird.flag) chips.appendChild(node("span", "chip strong", "Rare here"));
    chips.appendChild(node("span", "chip", `${shortPlace(bird.spot.name)} · ${bird.spot.distanceKm} km`));
    body.appendChild(evidenceMeter(bird.evidence));

    const where = shortPlace(bird.spot.name, true);
    let fact = bird.ebird?.day ? `Last reported on eBird ${shortDate(bird.ebird.day)} at ${where}.` : `Not on eBird at ${where} in this period.`;
    if (bird.ebird?.checklists) fact += ` On ${bird.ebird.checklists} checklist${bird.ebird.checklists === 1 ? "" : "s"}, ${bird.ebird.confirmed ? "confirmed by a reviewer" : "not yet confirmed by a reviewer"}.`;
    fact += bird.inat ? ` iNaturalist has ${bird.inat.count} observation${bird.inat.count === 1 ? "" : "s"} nearby.` : " iNaturalist has none nearby.";
    body.appendChild(node("p", "bird-fact", fact));

    if (bird.months) body.appendChild(seasonChart(bird));

    const links = body.appendChild(node("div", "bird-links"));
    if (bird.ebird?.source) links.appendChild(link("eBird checklist", bird.ebird.source));
    if (bird.inat?.source) links.appendChild(link("iNaturalist records", bird.inat.source));
    if (bird.historySource) links.appendChild(link("Season history", bird.historySource));

    const toggle = body.appendChild(node("button", "seen-toggle"));
    toggle.type = "button";
    const paint = () => {
        const isSeen = key in seen;
        card.classList.toggle("seen", isSeen);
        toggle.setAttribute("aria-pressed", String(isSeen));
        toggle.textContent = isSeen ? "✓ Found" : "Found it!";
    };
    toggle.addEventListener("click", () => setSeen(bird, !(key in seen)));
    // Repainted from the journal, so removing a stamp there un-marks the card here
    document.addEventListener("collection-changed", () => card.isConnected && paint());
    paint();

    body.appendChild(credit);
    return card;
}

// --- The quest: the plan as something to go and do ---

function questCard(run) {
    if (!run.spots.length) return null;
    const quest = node("section", "quest");
    const top = run.birds[0];
    const nearest = [...run.spots].sort((a, b) => a.distanceKm - b.distanceKm)[0];

    quest.appendChild(node("div", "quest-eyebrow", run.date ? "Field report" : "Today's quest"));
    const title = run.date
        ? `What was reported on ${shortDate(dayOf(run.date))}`
        : top
          ? `Find the ${top.species} at ${shortPlace(top.spot.name, true)}`
          : `Explore ${shortPlace(nearest.name, true)}`;
    quest.appendChild(node("h3", "quest-title", title));
    const meta = quest.appendChild(node("div", "quest-meta"));
    const count = () => {
        const counts = [`${run.spots.length} spot${run.spots.length === 1 ? "" : "s"} checked`];
        if (run.birds.length) counts.push(`${run.birds.length} bird${run.birds.length === 1 ? "" : "s"} to look for`);
        counts.push(`nearest spot ${nearest.distanceKm} km away`);
        const found = run.birds.filter((bird) => birdKey(bird) in seen).length;
        if (found) counts.push(`${found} of ${run.birds.length} found`);
        meta.textContent = counts.join(" · ");
    };
    document.addEventListener("collection-changed", () => quest.isConnected && count());
    count();

    // The page puts the plan's opening paragraph here: the Writer's own answer, with its sources
    quest.appendChild(node("div", "quest-brief"));

    if (run.birds.length) {
        quest.appendChild(node("div", "quest-head", run.date ? "Birds in the records" : "Your targets"));
        if (!run.date) {
            const legend = quest.appendChild(node("div", "evidence-legend"));
            legend.appendChild(node("span", "", "How well the records back each bird: "));
            for (let level = 3; level >= 0; level--) {
                const item = legend.appendChild(node("span", "key"));
                const dots = item.appendChild(node("span", "evidence-dots"));
                for (let i = 1; i <= 3; i++) dots.appendChild(node("span", "evidence-dot" + (i <= level ? " on" : "")));
                item.appendChild(node("span", "", EVIDENCE[level].toLowerCase()));
            }
            legend.appendChild(node("span", "evidence-caveat", "A label describes the records. It is not your odds of seeing the bird."));
        }
        const cards = quest.appendChild(node("div", "bird-cards"));
        for (const bird of run.birds) cards.appendChild(birdCard(bird));
    }
    return quest;
}

// --- Linked views: the cards, the timeline and the map point at each other ---

function linkViews(stage, quest, timeline) {
    const cards = [...quest.querySelectorAll(".bird-card")];
    const rows = timeline ? [...timeline.querySelectorAll(".tl-row[data-bird]")] : [];
    const both = [...cards, ...rows];

    // Pointing at a bird, on its card or its timeline row, lights its spot and its observations on the map
    const light = (from) => {
        const names = from ? [from.dataset.bird, from.querySelector("h4, .tl-name span")?.textContent ?? ""] : null;
        stage.highlight(from ? { spotId: from.dataset.spot, names } : null);
        for (const other of both) other.classList.toggle("lit", Boolean(from) && other.dataset.bird === from.dataset.bird);
    };
    for (const each of both) {
        each.addEventListener("pointerenter", () => light(each));
        each.addEventListener("pointerleave", () => light(null));
        each.addEventListener("focusin", () => light(each));
        each.addEventListener("focusout", () => light(null));
    }

    // Picking a pin on the map shows only that spot's birds. Picking the empty map shows them all again.
    const grid = quest.querySelector(".bird-cards");
    if (!grid) return;
    const note = node("div", "filter-note");
    note.hidden = true;
    grid.before(note);
    const show = (locationId, name) => {
        const here = locationId ? cards.filter((card) => card.dataset.spot === locationId) : [];
        for (const card of cards) card.hidden = here.length > 0 && !here.includes(card);
        note.hidden = !locationId;
        note.replaceChildren();
        if (!locationId) return;
        note.appendChild(node("span", "", here.length ? `Showing the ${here.length === 1 ? "bird" : `${here.length} birds`} at ${shortPlace(name)}.` : `None of these birds are at ${shortPlace(name)}.`));
        if (here.length) {
            const all = note.appendChild(node("button", "", "Show all"));
            all.type = "button";
            all.addEventListener("click", () => show(null));
        }
    };
    stage.onPick(show);
}

// --- The picker: say where you are by clicking a map ---
//
// The ring is how far the visitor might get on foot: a third of their time each way, at 80 m a
// minute, less a quarter because streets are not straight lines. It is an estimate, and the
// page calls it one. box is the element to draw in; onChange gets {lat, lng, minutes, reachKm}.

function createPicker(box, onChange) {
    if (!window.L) return null;
    const map = L.map(box, { scrollWheelZoom: false }).setView([40.7831, -73.9712], 12);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    let point = null;
    let minutes = 60;
    let pin = null;
    let ring = null;
    const reachKm = () => (minutes / 3) * 0.08 * 0.75;
    const redraw = () => {
        if (!point) return;
        if (!pin) {
            pin = L.marker(point, { icon: L.divIcon({ className: "", html: '<span class="shape start on-map"></span>', iconSize: [24, 24], iconAnchor: [12, 12] }), keyboard: false }).addTo(map);
            ring = L.circle(point, { radius: 1, color: "#2f6b3f", weight: 1.5, fillColor: "#2f6b3f", fillOpacity: 0.12, interactive: false }).addTo(map);
        }
        pin.setLatLng(point);
        ring.setLatLng(point);
        ring.setRadius(reachKm() * 1000);
        map.fitBounds(ring.getBounds().pad(0.35));
        onChange({ lat: +point[0].toFixed(5), lng: +point[1].toFixed(5), minutes, reachKm: +reachKm().toFixed(1) });
    };
    map.on("click", (event) => {
        point = [event.latlng.lat, event.latlng.lng];
        redraw();
    });
    return {
        setMinutes(value) {
            minutes = value;
            redraw();
        },
        setPoint(lat, lng) {
            point = [lat, lng];
            redraw();
        },
        // The map sits in a folded section: it has no size to measure until that opens
        refresh() {
            map.invalidateSize();
        },
    };
}
