# Wise Sayings: Sayings Compendium Engine

A small offline tool that stores, tags and queries a curated compendium of
high-impact sayings (apophthegms, epigrams, aphorisms, maxims, proverbs) and
uses them as a decision aid. Describe a situation; get back the most relevant
sayings, what each one makes you see, and the sayings that pull against each
other.

Design spec: [docs/design-spec.md](docs/design-spec.md).

## Setup

Python 3.9+ only. No packages to install, no server, no network. Everything
lives in this folder: copy it anywhere and it works.

```
python sayings.py --help
```

The database (`sayings.db`) is created on first use. The tag vocabulary is
`tags.txt`; edit it freely, and it is re-read on every command.

### AI calls go through claude_watcher

The only features that call Claude are `consult --smart` and the sphere's
meaning scores. They go through **claude_watcher**
(`D:\Projects\claude_watcher`), Phil's HTTP service that runs headless Claude
Code under his subscription, so calls aren't billed per token. The client is
`compendium/watcher.py`, which uses only the standard library. It needs one setting,
`WATCHER_TOKEN`, from the environment or from a `.env` file in this folder.
`WATCHER_URL` is optional and must be https.

- **`.env` holds secrets and is git-ignored. Never commit it.**
- The watcher is shared with Network Meaning: it runs one call at a time and
  allows 200 calls a day for all apps. Calls here are labelled
  `caller: "wise-sayings"`. When the watcher is busy the client waits and
  retries (20 s, 45 s, 90 s); a `daily_limit` answer stops the run, without
  retrying. Any run that would need more than 10 calls asks for `--yes`, and is
  refused if it would leave fewer than 50 of today's calls for other apps. That
  is checked with the watcher's `GET /usage` (watcher 0.9.0+).
- The watcher runs Claude Code non-interactively with no tools, so a call can't
  stall on a permission prompt; at worst it times out after about 5½ minutes.
- If the watcher can't be reached, each feature falls back to its offline method
  and says so.
- `SAYINGS_TRANSPORT=api` switches to the Anthropic SDK and an
  `ANTHROPIC_API_KEY` instead (`pip install anthropic`).

## The no-fabrication rule

Every saying must demonstrably exist outside the model. Imports are refused
unless `documented_in` names where it is recorded (collection, work, passage if
known). Attribution is tracked separately from existence:

| attribution_confidence | meaning |
|---|---|
| `verified` | in the person's own writings or a contemporary record |
| `attributed_uncertain` | the traditional attribution, but sources are late, reconstructed, or disputed (most ancient anecdotes) |
| `misattributed` | the saying is real but the popular attribution is false (kept, and marked) |
| `anonymous` | proverb with no author; `attribution` must be empty |

A saying whose existence can't be confirmed is rejected, not kept as "probable".

## Workflow

New imports are **accepted** by default (the owner's choice, 2026-10-28: every
saying counts as accepted until un-accepted). Un-accept with `set-status`. To go
back to the original review-first flow, run `python sayings.py settings default_status candidate`.

```
# Import a batch (JSON or CSV). The whole batch is validated first;
# one bad record aborts the import so nothing half-loads.
python sayings.py import seed/batch2.json

# Un-accept (or reject) sayings by id
python sayings.py set-status candidate 12 40
python sayings.py set-status rejected 71 --note "weak lens"

# Optional review round-trip, for sayings left as candidates
python sayings.py review export review.md     # fill in each "Decision:" line
python sayings.py review apply review.md

# Ask for help with a situation
python sayings.py consult "A much bigger competitor wants to buy us and threatens a price war if we refuse"

# Lay sayings out on a sphere and open it in the browser
python sayings.py sphere run --n 15 --open
```

## Commands

| command | what it does |
|---|---|
| `import FILE` | add a JSON or CSV batch (status from `default_status`) |
| `add --text ... --form ... --confidence ... --documented-in ... --lens ... --impact N --rationale ... --situation TAG` | add one saying |
| `set-status accepted\|candidate\|rejected ID... [--note TEXT]` | accept, un-accept, or reject sayings |
| `settings [default_status [VALUE]]` | show or change settings |
| `sphere run [--n N] [--ids 1,2,3] [--seed S] [--open]` | lay out sayings on a sphere, save the run, write `export/sphere-<run>.html` |
| `sphere run --stage meaning [--scorer claude\|words]` | stage two layout, by meaning |
| `sphere compare [RUN] [--n N\|--ids\|--all] [--open]` | tags vs meaning on one sample: two globes plus a report of hidden connections and possible mis-tags |
| `sphere score [--n N\|--ids\|--all] [--yes]` | have Claude score pairs ahead of time (cached) |
| `sphere list` / `sphere render RUN [--open]` | list saved runs / redraw one |
| `review export [FILE]` / `review apply FILE` | markdown review round-trip |
| `search [TEXT] [--tag T] [--form F] [--by NAME] [--status S] [-v]` | find sayings (accepted only by default; `--status all` for everything) |
| `show ID` | one saying in full |
| `consult "SITUATION" [--limit N]` | 10–15 most relevant accepted sayings, grouped by situation, with lenses and counter-saying tensions |
| `export [--format md\|json\|both] [--out-dir DIR]` | full compendium to `export/compendium.md` and `.json` |
| `stats [--status S]` | counts by status, form, confidence, tag |
| `tags` | list the vocabulary |
| `dedupe [--threshold 0.6]` | list near-duplicate wordings |

Global options: `--db PATH`, `--vocab PATH`.

## Import format

JSON is a list of objects; CSV has the same column names, with list fields
separated by `;`.

```json
{
  "text": "Bad is the plan that cannot be changed.",
  "variants": ["Malum est consilium quod mutari non potest."],
  "form": "maxim",
  "attribution": "Publilius Syrus",
  "attribution_confidence": "verified",
  "documented_in": "Publilius Syrus, Sententiae (1st century BC)",
  "lens": "Before committing, ask what would make you change course and whether you still could.",
  "impact_score": 8,
  "impact_rationale": "Core of contingency planning for 2,000 years.",
  "subject_tags": ["planning", "change"],
  "situation_tags": ["plan_failing", "commitment"],
  "counter_sayings": ["The die is cast."]
}
```

`counter_sayings` may name an existing id (`12` or `"#12"`) or the exact text
of another saying in the database or the same batch. Counter links are
symmetric. At least one situation tag is required, since `consult` depends on it.

## How consult works

**v1, the default, runs offline.** The situation text is matched against each
tag's name and the aliases in `tags.txt` (crude stemming, so "negotiating"
matches "negotiate"). Matching situation tags weigh 3 and subject tags weigh 2.
Words that also appear in a saying's text or lens add a little. The score is
relevance × impact_score. Results are grouped by their strongest matching tag,
and any counter-pairs that touch the results are shown as tensions. The counter
is pulled in even if it didn't rank.

**To get better matching**, add aliases to `tags.txt`. That is the main tuning knob.

**v2 (optional) is smart matching with Claude.** Keyword matching only sees
words. For "my cofounder keeps meeting with our biggest rival behind my back"
it finds *competition* but misses *betrayal*. `--smart` sends the situation and
the tag list to Claude, which picks the tags that fit and rates each one 1–3.
Ranking still happens locally against your accepted sayings, so Claude can't
add or invent a saying.

```
python sayings.py consult --smart "my cofounder keeps meeting with our biggest rival behind my back"
```

It makes one call through claude_watcher (see Setup) with `claude-opus-5-5`;
override the model with `SAYINGS_MODEL`. If the watcher can't be reached, it
prints a note and uses keyword matching, so the tool still works offline.

You can also write your own matcher: a function
`match(situation, tags) -> {tag_id: weight}`, where each tag is a dict with
`id, name, kind, aliases`. Enable it with `--matcher mymodule:match` or
`SAYINGS_MATCHER=mymodule:match`.

## The sayings sphere

Spec: [docs/sphere-layout-spec.md](docs/sphere-layout-spec.md). Both stages,
the comparison, and the sphere centred on a situation are built.

### Centred on your situation

```
python sayings.py consult "a much bigger competitor wants to buy us out and threatens a price war" --sphere --open
```

This makes the decision-aid view. `consult` finds the best sayings for your
situation (`--n`, default 15; add `--smart` to have Claude pick the tags).
The counter-sayings of those matches are added too. Then:

- Your situation is a fixed white marker near the top of the globe, with
  dashed rings at 30°, 60° and 90° from it.
- Each saying is pulled toward it in proportion to its relevance, so the
  closer a saying sits, the better it fits.
- Among themselves the sayings arrange by meaning (Claude scores via the
  watcher, cached), or by tags with `--stage tags` or if the watcher can't be
  reached.
- Counter-sayings are pushed to the far side of what they argue against.

Click the marker for the sayings ranked by relevance and the tags your
situation matched. Click a saying for its lens and its relevance (or why it's
there, if it was pulled in as a counter). The run prints how well relevance
tracks distance; it's usually a correlation around −0.8.

```
python sayings.py sphere run --n 15 --open                  # stage one: by tags; any number up to all sayings
python sayings.py sphere run --stage meaning --n 15 --open  # stage two: by meaning
python sayings.py sphere compare --n 20 --open              # both on the same sample, side by side, plus a report
python sayings.py sphere compare 4 --open                   # compare using run 4's sample and seed
python sayings.py sphere score --all                        # pre-score every pair with Claude (cached)
python sayings.py sphere run --ids 11,12,20,5,24 --seed 3
python sayings.py sphere list
python sayings.py sphere render 1 --open
```

How a layout is made:

1. Scatter the N sayings at random points on a unit sphere.
2. Relax under three forces until the energy stops dropping:
   every pair repels (Thomson problem), so points spread evenly;
   related pairs attract with strength `--pull` × relatedness;
   counter-sayings are pushed toward opposite sides (`--push-counters`),
   and their tag attraction is switched off, because opposites often share tags.
   This repeats from `--restarts` random starts and keeps the lowest-energy result.
3. Triangulate: the convex hull of the points, which for points on a sphere is
   the spherical Delaunay triangulation (always 3N−6 edges).
4. Snap each saying to the nearest free point of the 1,002-point frequency-10
   geodesic lattice (`--no-snap` to skip). Points move about 4° at most.
5. Save the run and write a page you open in a browser: a self-contained HTML
   file with Three.js inlined, so it works offline with no server.

Stage one relatedness is the Jaccard overlap of the two sayings' subject and
situation tags. `--form-weight 0.3` makes 30% of it come from sharing a form.
Each run prints how well it worked: the average angle between related pairs
versus unrelated ones, the closest pair, and how far apart the counter-sayings
ended up (180° means opposite sides).

In the page, drag to turn, use the wheel to zoom, and click a saying for its
lens, its neighbours, and the sayings that pull the other way; click it again
to close the panel. Labels are
shortened to about 30 characters; hover over a saying (or the situation marker)
for a popup with the full text and attribution. The popup rides along with the
saying as the globe turns, fades as it passes behind a see-through globe, and
disappears once the globe hides it. Lines brighten
with relatedness; orange dashes run through the globe between counter-sayings.

**Defaults.** A page opens with its controls panel showing and a 96%-opaque
grey-green globe (rgb 71, 81, 71) with the sayings, their labels, the triangle
mesh, the situation and the meaning axes on. The
lattice, distance rings and the chords through the inside (counter-sayings,
hidden connections, mis-tags) start off; switch them on in the panel, or use the
Everything or Tensions preset.

**Controls** (open at load; the button at bottom left or `C` hides them). Every kind of element on
the page has its own row: an on/off switch, a colour picker (double-click to
reset), and a slider from 0% (invisible) to 200%. Below 100% the element dims,
and above it brightens: more opaque, and gently toward white. The rows are:

- Structure: globe shell, lattice marbles, triangle mesh, distance rings
- Sayings: one row per form (apophthegms, aphorisms, maxims, proverbs, epigrams), labels
- Relations: counter-saying chords, hidden connections, possible mis-tags
- Situation: marker, label

Only rows for things the page actually has are shown.

The **globe surface** row's slider is opacity instead of brightness: 0% is clear
glass and 100% is solid. Solid really is solid: you see only the near side,
with the sayings, mesh and labels sitting on top of the surface, and nothing
behind showing through. The counter-saying chords run through the inside, so
they're hidden too. Clicks on the surface don't reach sayings behind it.
Between 0% and 100%, far-side labels fade in step. `O` toggles between solid and
your last see-through setting, and the Solid and Glass presets set it.

Below the rows:

- Filters:
  - hide mesh links weaker than a threshold;
  - on a situation sphere, hide sayings below a relevance threshold, and show
    or hide the pulled-in counter-sayings;
  - labels: all, front side only, or the selected saying and its neighbours;
  - saying size;
  - fade what isn't linked to the selected saying;
  - slow spin;
  - background colour.
- Presets: Default (96% opaque, minimal), Everything (all on, see-through), Sayings (quiet structure), Tensions (only sayings and the
  lines between opposites), Structure (mesh and lattice up, labels off), Clean, Solid, Glass.

Clicking a form in the legend toggles it. `L` toggles labels, `O` toggles a
solid globe, and `Esc` clears the selection. Settings are saved in the browser and carry over to every
sphere page; the thresholds stay per page. A link can open the panel or apply a
preset: `sphere-4.html#controls`, `sphere-4.html#preset=Tensions`. Comparison
pages apply the settings to both globes.

### Meaning axes

```
python sayings.py sphere rate-axes --all                     # Claude rates every saying on every axis (cached)
python sayings.py sphere run --all --stage meaning --axes --open
python sayings.py consult "your situation" --sphere --axes --open
```

`axes.txt` names up to 6 axes, one per line: `key: Thesis | Antithesis | what it is about`.
Each axis is a rod through the globe's centre whose ends stand out about 20% of
the radius above the surface (about two inches on a 20-inch globe), with the thesis at one end and the
antithesis at the opposite end. The rods are exactly equally spaced:

- 4–6 axes use the icosahedron's corner pairs, 63.4° between neighbours. That
  is the same icosahedron the lattice is built from, so every pole is a lattice
  point.
- 1–3 axes use x, y and z.

The axes in use:

| Thesis | Antithesis |
|---|---|
| Act boldly | Act with caution |
| Hold your course | Adapt and bend |
| Force | Guile |
| Power decides | Legitimacy decides |
| Trust others | Guard against others |
| Change what you can | Accept what you can't |

Claude rates each saying on each axis: −2 (strongly the antithesis), −1, 0
(not about this axis), +1, or +2 (strongly the thesis). That is 25 sayings per
watcher call, 7 calls for all 151, cached in `axis_scores`. Editing an axis or
a saying re-rates only what changed. In the layout each saying is pulled toward
the pole it leans to (`--axis-pull`, default 1.5; 1.0 on situation spheres),
so sayings with opposite meanings end up facing each other across the globe.
The poles only attract; they don't push anything away. Each run prints, per
axis, the mean angle of its sayings to their own pole and to the opposite one.
For all 151 sayings that was 50–76° versus 104–130°.

In the viewer:

- The rods and their labels each have a controls row.
- Every pole (thesis or antithesis) is the same slim twig-brown cylinder,
  standing out of the surface along its rod, so no pole can be mistaken for a
  saying. The label tells the ends apart: upright for the thesis, italic for
  the antithesis.
- Click a pole to list the sayings that lean toward it.
- Each saying's panel shows how it leans.
- On a solid globe the rods vanish inside, and only the tips show above the
  surface.

The first ratings also show a lean in the collection: caution outnumbers
boldness about 58 to 26, and no saying is rated strongly for *Force*. That is
worth knowing when gathering more.

### Stage two: meaning

Relatedness is how close two sayings are in meaning: their insight, their
mechanism, or the kind of situation they speak to, judged from text and lens and
ignoring tags. Scorers are pluggable (`--scorer`):

- **`claude`** (default): Claude (`claude-opus-5-5`; override with `SAYINGS_MODEL`)
  reads the sayings and rates each pair 0–10, up to 100 pairs per call, through
  claude_watcher (see Setup). Scores are cached in `relatedness_cache`, keyed by
  model and a fingerprint of each saying's text and lens. Repeat layouts are
  free and work offline, and editing a saying re-scores only its own pairs.
  A 15-saying sample is 105 pairs (2 calls); 20 sayings is 190 pairs (2 calls).
  All 151 sayings are 11,325 pairs (114 calls, over half the watcher's shared
  daily limit), so do that in stages at a quiet time. More than 10 calls needs `--yes`.
- **`words`**: an offline TF-IDF word-overlap score. It needs nothing, and it is
  what `claude` falls back to, with a note, when the watcher can't be reached. It sees shared words, not shared ideas, so the page and report carry
  a warning when it was used.

Anthropic has no embeddings endpoint, so Claude scores pairs directly rather
than comparing vectors. An embeddings scorer (e.g. Voyage AI) can be added to
`SCORERS` in `compendium/meaning.py`; it only has to return pair scores from 0 to 1.

Each run records which scorer it used.

### Comparison

`sphere compare` lays out the same sample both ways with the same seed. It then
writes `export/compare-<id>.md` and `export/compare-<id>.html`. The page shows
the two globes side by side, turning together; the tags layout is rotated (and
mirrored if that fits better) to line up with the meaning layout. The report
gives:

- **Agreement:** rank correlation between the tag and meaning scores over all
  pairs. Ranks are used because the two scales aren't comparable.
- **Hidden connections** (gold arcs): pairs in the top 10% by meaning that share
  no tags. These are the non-obvious neighbours, and candidates for a shared tag.
- **Possible mis-tags** (purple arcs): pairs sharing at least 40% of their tags
  but in the bottom 25% by meaning.

The report is computed from the scores, not from positions on the globe:
positions depend on the random start and on compromises between forces.

**Borrowed from Network Meaning** (`D:\Projects\network-meaning`, read only, not modified):
- `compendium/geodesic.py` ports the icosahedral marble frame (`MF`) from
  `public/sphere.html`. Same corners, faces and order, so vertex numbers match.
  Its default frame, Fuller's lattice in quadrays, isn't used: that frame's
  outer layer is a cuboctahedron, not a sphere.
- `compendium/sphere_template.html` follows the same file's drawing approach:
  a world group turned by dragging, instanced marbles, labels projected each
  frame and dimmed on the far side, and the same palette.
- `compendium/vendor/three-r128.min.js` is copied unchanged from `public/vendor/` (MIT license).

## Published pages

Sphere pages are published to the website's demos folder
(`D:\AWS3\websites\toughguycomputing_net\www\demos`) as `sayings-<file>`,
alongside other projects' demos, which are never touched. After publishing, run

```
python tools/build_demos_index.py
```

to rebuild https://www.toughguycomputing.com/demos/sayings-index.html and the
shared page list `sayings-nav.js`. The index lists only this project's pages,
grouped as situation spheres, comparisons and plain spheres. Each entry comes from the page's own `<title>` and
`<meta name="description">`, which `compendium/render.py` writes.

Every page has a **Pages** menu at the top (or press `P`) listing all the pages,
grouped as in the index, with the current one marked. Use ‹ › or `[` `]` for
the previous and next page. Pages read the list from `sayings-nav.js` when they
open, so older pages show newly published ones without being redrawn. The
local `export\` folder gets its own list and index each time a page is written,
so the menu works there too. `#pages` in a link opens the menu.

## Database schema versions

The schema is versioned with SQLite's `PRAGMA user_version`. Migrations live in
`compendium/db.py` (`MIGRATIONS`) and run automatically on every command, so an
older `sayings.db` is upgraded in place without losing data.

| version | adds |
|---|---|
| 1 | `sayings`, `tags`, `saying_tags`, `counter_pairs` (the original schema) |
| 2 | `settings` (key/value, e.g. `default_status`); `layout_runs` (stage, seed, sample, parameters, energy, quality stats); `layout_points` (each saying's relaxed x,y,z and snapped lattice vertex); `layout_edges` (triangulation edges) |
| 5 | `axis_scores` (each saying's −2..+2 rating on each meaning axis, per model, keyed by axis and saying fingerprints) |
| 4 | `layout_runs.center` (for a consult sphere: the situation, each saying's relevance, matched tags, the situation's position and lattice vertex; edges to the situation use id 0) |
| 3 | `relatedness_cache` (Claude meaning scores per pair, keyed by model and saying fingerprints); `layout_edges.relatedness` (each edge's score in its run); `comparisons` (tags run, meaning run, report) |

## Tests

```
python -m unittest discover tests
```

## Layout

```
sayings.py            CLI entry point
compendium/           db (schema, migrations, vocab sync), records (validate/import/search/dedupe/status),
                      review, consult, llm_matcher (--smart), report (export/stats),
                      sphere (layout, alignment), meaning (stage two scorers, comparison),
                      geodesic (lattice), render + sphere_template.html (pages), cli
compendium/vendor/    three-r128.min.js (from Network Meaning)
tags.txt              editable tag vocabulary + consult aliases
seed/                 starter.json (batch 1), batch2.json
docs/                 design-spec.md, sphere-layout-spec.md
export/               generated pages and exports (not committed)
tests/                unittest suite
```
