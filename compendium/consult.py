"""consult: map a free-text situation to tags, then rank accepted sayings.

v1 is offline keyword matching. A matcher is any function

    matcher(situation: str, tags: list[dict]) -> dict[int, float]

that returns {tag_id: weight}. Each tag dict has id, name, kind, aliases.
To plug in an embeddings/LLM matcher (v2), set SAYINGS_MATCHER=module:function
or pass --matcher module:function. The default needs no network.
"""

import importlib
import json
import os
import re

from . import records

SITUATION_WEIGHT = 3.0
SUBJECT_WEIGHT = 2.0
TEXT_HIT_WEIGHT = 0.5   # per situation word found in the saying's lens/text
TEXT_HIT_CAP = 4        # so long lenses don't swamp tag matches
MIN_TEXT_ONLY_HITS = 2  # a saying with no tag match needs this many word hits


def _stem(word):
    for suf in ("ations", "ation", "ings", "ing", "ions", "ion", "ers", "er",
                "ies", "es", "ed", "ly", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            word = word[: -len(suf)]
            break
    if word.endswith("y") and len(word) > 4:   # worry / worried -> worri
        word = word[:-1] + "i"
    return word


def tokens(text, keep_stopwords=False):
    words = re.findall(r"[a-z]+", text.lower())
    return [_stem(w) for w in words if keep_stopwords or w not in records.STOPWORDS]


def _same(a, b):
    """Stems match exactly, or one is a prefix of the other (both >= 5 chars)."""
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a))


def _phrase_in(phrase_tokens, situation_tokens):
    return bool(phrase_tokens) and all(any(_same(p, s) for s in situation_tokens) for p in phrase_tokens)


def keyword_matcher(situation, tags):
    # Keep stopwords so an alias like "no competition" needs the "no" too.
    sit = tokens(situation, keep_stopwords=True)
    weights = {}
    for tag in tags:
        phrases = [tag["name"].replace("_", " ")] + tag["aliases"]
        if any(_phrase_in(tokens(p, keep_stopwords=True), sit) for p in phrases):
            weights[tag["id"]] = SITUATION_WEIGHT if tag["kind"] == "situation" else SUBJECT_WEIGHT
    return weights


def load_matcher(spec=None):
    spec = spec or os.environ.get("SAYINGS_MATCHER")
    if not spec:
        return keyword_matcher
    module, _, func = spec.partition(":")
    return getattr(importlib.import_module(module), func or "match")


def consult(conn, situation, limit=12, matcher=None):
    """Return (groups, tensions, matched_tags).

    groups:   [(tag_name, [saying, ...])] ordered by best score
    tensions: [(saying_a, saying_b, both_in_results)] counter-pairs touching
              the results; the counter is pulled in even if it didn't rank
    """
    matcher = matcher or keyword_matcher
    tags = [dict(r, aliases=json.loads(r["aliases"])) for r in
            conn.execute("SELECT id, name, kind, aliases FROM tags")]
    tag_names = {t["id"]: t["name"] for t in tags}
    tag_weights = matcher(situation, tags)
    sit_tokens = set(tokens(situation))

    scored = []
    for row in conn.execute("SELECT id, text, lens, impact_score FROM sayings WHERE status='accepted'"):
        my_tags = [r["tag_id"] for r in conn.execute(
            "SELECT tag_id FROM saying_tags WHERE saying_id=?", (row["id"],))]
        hits = [(tag_weights[t], t) for t in my_tags if t in tag_weights]
        body = set(tokens(row["text"] + " " + row["lens"]))
        text_hits = sum(1 for w in sit_tokens if any(_same(w, b) for b in body))
        if not hits and text_hits < MIN_TEXT_ONLY_HITS:
            continue
        relevance = sum(w for w, _ in hits) + TEXT_HIT_WEIGHT * min(text_hits, TEXT_HIT_CAP)
        primary = tag_names[max(hits)[1]] if hits else "related wording"
        scored.append((relevance * row["impact_score"], row["id"], primary, relevance))

    scored.sort(key=lambda s: (-s[0], s[1]))
    top = scored[:limit]
    result_ids = {sid for _, sid, _, _ in top}

    groups, order = {}, []
    for score, sid, primary, relevance in top:
        if primary not in groups:
            groups[primary] = []
            order.append(primary)
        s = records.get(conn, sid)
        s["score"] = round(score, 1)          # relevance x impact: the ranking
        s["relevance"] = round(relevance, 2)  # how well it fits the situation, before impact
        groups[primary].append(s)

    tensions, seen = [], set()
    for _, sid, _, _ in top:
        for other in records.counters_of(conn, sid):
            pair = tuple(sorted((sid, other)))
            if pair in seen:
                continue
            o = records.get(conn, other)
            if o["status"] != "accepted":
                continue
            seen.add(pair)
            tensions.append((records.get(conn, pair[0]), records.get(conn, pair[1]),
                             pair[0] in result_ids and pair[1] in result_ids))

    matched = sorted(tag_names[t] for t in tag_weights)
    return [(name, groups[name]) for name in order], tensions, matched
