# SIDE ARTIFACT — Sayings Compendium Engine

**Status: Side artifact. NOT part of the Network Meaning game/synthesis rounds.** Lives in this folder for convenience only. Do not mix with round docs.

## Purpose

Build a standalone, self-contained tool that gathers, stores, tags and queries a curated compendium of high-impact sayings — apophthegms, epigrams, aphorisms, maxims, proverbs. Target: roughly 300–500 of the very best, measured by impact. Primary use: a decision aid. User describes an uncertain situation; the tool returns the most relevant sayings, each with what that lens reveals about the situation.

Precedents for the idea: Plutarch's Apophthegmata (Sayings of Kings and Commanders, Sayings of Spartans), Erasmus's Adagia, La Rochefoucauld's Maximes. Example entry: Philip II — no fortress is impregnable if a donkey laden with gold can reach it.

## Hard rule: no fabrication

- A saying does NOT need an identifiable author. Anonymous proverbs are fine.  
- A saying MUST demonstrably exist outside the model — in a published collection, book, or established quotation record. Nothing invented, paraphrased into novelty, or "in the style of."  
- Every record carries an existence note: where it is documented (collection, work, passage if known).  
- Misattribution is common (Twain especially). Record attribution confidence separately from existence. A real saying with a false attribution stays in, marked as such.  
- If existence cannot be confirmed, the record is rejected, not kept as "probable."

## Deliverable

Standalone artifact: single project directory, Python 3, SQLite (one .db file), CLI. No server, no external services required to run queries. Portable — copy the folder and it works.

## Data model (table: sayings)

- id — integer primary key  
- text — the saying, canonical wording  
- variants — known alternate wordings (optional)  
- form — one of: apophthegm, epigram, aphorism, maxim, proverb  
- attribution — person, or null if anonymous  
- attribution\_confidence — verified, attributed\_uncertain, misattributed, anonymous  
- documented\_in — evidence it exists (collection/work/passage)  
- subject\_tags — e.g. power, money, war, human\_nature, truth, timing, risk  
- situation\_tags — situations it applies to, e.g. negotiation, betrayal, acting\_with\_incomplete\_info, overwhelming\_opponent, temptation, leadership\_crisis  
- lens — 1–3 sentences: what this saying makes you see in a situation  
- impact\_score — 1–10  
- impact\_rationale — short reason for the score  
- counter\_sayings — ids of sayings that pull the opposite way (e.g. "look before you leap" vs "he who hesitates is lost"); contradictions are a feature  
- status — candidate, accepted, rejected  
- added\_at

Tags should live in a normalized tag table with a join table so the vocabulary stays controlled. Keep a tags vocabulary file the user can edit.

## Impact scoring criteria

- Does it describe a real strategy or truth that has proven out?  
- Has it endured across centuries/cultures?  
- Does it change how you see a hard situation (lens value), or is it merely clever?  
- Is it non-redundant with sayings already accepted? Reject merely memorable lines that offer no lens.

## Gathering workflow

1. Generate candidates in batches by source tradition: Plutarch (kings, commanders, Spartans), Diogenes Laertius (philosophers), Suetonius, Herodotus/Thucydides, Roman (Cicero, Seneca, Publilius Syrus), Erasmus's Adagia, La Rochefoucauld, Franklin, Twain, world proverb traditions (Chinese, Arabic, Persian, etc.), modern strategists.  
2. Each candidate enters as status=candidate with documented\_in filled.  
3. Batch review export (markdown) for the user to accept/reject.  
4. Accepted records become queryable.  
5. Dedupe: flag near-duplicate ideas; keep the strongest wording.

## CLI commands

- add / import (JSON or CSV batch)  
- review — export candidates for review; apply accept/reject decisions from file  
- search — by text, tag, form, attribution  
- consult "situation description" — returns the 10–15 most relevant accepted sayings, grouped, with lens text, and surfaces counter-sayings in pairs to expose tensions  
- export — full compendium to markdown and JSON  
- stats — counts by form, tag, confidence

## consult implementation

v1: keyword/tag matching against situation\_tags and lens text, ranked by relevance × impact\_score. v2 (optional, pluggable): embeddings or an LLM call to map a free-text situation to tags and rank. Must remain optional so the artifact runs offline.

## Constraints

- Keep it simple and self-contained; minimal dependencies (stdlib \+ sqlite3 preferred).  
- Include a README with setup and example commands.  
- Seed with a small verified starter batch (\~20) to prove the pipeline, then stop for user review before bulk gathering.