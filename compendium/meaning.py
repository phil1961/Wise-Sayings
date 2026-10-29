"""Stage two relatedness: how close two sayings are in meaning (docs/sphere-layout-spec.md).

Scorers are pluggable. Each returns {(a_id, b_id): score in 0..1} for a_id < b_id.

  claude  Claude reads the sayings (text + lens) and rates each pair 0-10. Scores are
          cached in relatedness_cache, keyed by model and a fingerprint of each saying,
          so layouts recompute offline and only new or edited sayings cost anything.
          Needs `pip install anthropic` and an API key.
  words   Offline fallback: TF-IDF cosine over text, variants and lens. Crude (it sees
          shared words, not shared ideas), but free and always available.

Anthropic has no embeddings endpoint, so 'claude' scores pairs directly instead of
comparing vectors. A vector scorer (e.g. Voyage AI) can be added to SCORERS later.
"""

import hashlib
import json
import math
import os
import sys

from . import consult, records

MODEL = os.environ.get("SAYINGS_MODEL", "claude-opus-5-5")
PAIRS_PER_REQUEST = 200


class ScorerUnavailable(Exception):
    """The scorer can't run here (no package, no key, no network...)."""


def _pairs(ids):
    ids = sorted(ids)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]


def fingerprint(s):
    return hashlib.sha1((s["text"] + "\n" + s["lens"]).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- words (offline)

def _doc(s):
    return " ".join([s["text"], *s["variants"], s["lens"]])


def word_scores(conn, ids, **_):
    """TF-IDF cosine. IDF comes from all accepted sayings so a score doesn't depend on the sample.

    Raw cosines are small, so they are divided by the sample's 95th percentile and capped
    at 1, to give springs a usable 0..1 range.
    """
    corpus = {r[0]: records.get(conn, r[0]) for r in conn.execute("SELECT id FROM sayings WHERE status='accepted'")}
    for i in ids:
        corpus.setdefault(i, records.get(conn, i))
    toks = {i: consult.tokens(_doc(s)) for i, s in corpus.items()}
    df = {}
    for t in toks.values():
        for w in set(t):
            df[w] = df.get(w, 0) + 1
    n = len(corpus)

    def vec(i):
        v = {}
        for w in toks[i]:
            v[w] = v.get(w, 0) + 1
        return {w: c * math.log((1 + n) / (1 + df[w])) for w, c in v.items()}

    vecs = {i: vec(i) for i in ids}
    norms = {i: math.sqrt(sum(x * x for x in v.values())) or 1.0 for i, v in vecs.items()}
    raw = {}
    for a, b in _pairs(ids):
        va, vb = vecs[a], vecs[b]
        raw[(a, b)] = sum(x * vb.get(w, 0) for w, x in va.items()) / (norms[a] * norms[b])
    vals = sorted(raw.values())
    top = vals[int(0.95 * (len(vals) - 1))] if vals and vals[-1] > 0 else 1.0
    top = top or vals[-1] or 1.0
    return {p: min(1.0, s / top) for p, s in raw.items() if s > 0}


# ---------------------------------------------------------------- claude

SYSTEM = """You judge how closely pairs of sayings (proverbs, maxims, aphorisms, apophthegms) are related in MEANING:
the insight, the mechanism, or the kind of situation they speak to, as an expert in proverbs and strategy would.

Ignore surface wording, culture of origin, era, form, and author. A Spartan apophthegm and a Yiddish proverb that
make the same point are closely related. Two sayings that give opposite advice about the same dilemma (e.g. act now
vs. wait) ARE closely related in meaning: they answer the same question.

Score each pair from 0 to 10:
  0  unrelated
  3  loosely connected (a shared theme, but different points)
  5  clearly share an idea or apply to the same kind of situation
  8  nearly the same insight, or direct answers to the same dilemma
  10 the same insight
Use the whole scale. Score every listed pair exactly once, using the bracketed ids."""


def _schema():
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer"},
                        "b": {"type": "integer"},
                        "score": {"type": "integer", "enum": list(range(11))},
                    },
                    "required": ["a", "b", "score"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    }


def _client():
    try:
        import anthropic
    except ImportError:
        raise ScorerUnavailable("the anthropic package is not installed (pip install anthropic)")
    try:
        return anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        raise ScorerUnavailable(str(e))


def _ask_claude(client, sayings, pairs):
    """One request: score `pairs` (list of (a, b)). Returns {(a, b): 0..10} for the pairs it answered."""
    try:
        import anthropic
    except ImportError:
        anthropic = None
    listing = "\n".join(f"[{i}] {s['text']}\n     Lens: {s['lens']}" for i, s in sorted(sayings.items()))
    asked = "\n".join(f"{a}-{b}" for a, b in pairs)
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if the model declines, the API retries on a recommended fallback model
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema()}},
            system=SYSTEM,
            messages=[{"role": "user", "content":
                       f"Sayings:\n{listing}\n\nScore these {len(pairs)} pairs (a-b):\n{asked}"}],
        ) as stream:
            response = stream.get_final_message()
    except ScorerUnavailable:
        raise
    except Exception as e:
        if anthropic is None or not isinstance(e, anthropic.APIError):
            raise
        if isinstance(e, anthropic.AuthenticationError):
            raise ScorerUnavailable("API key missing or invalid; set ANTHROPIC_API_KEY")
        if isinstance(e, anthropic.RateLimitError):
            raise ScorerUnavailable("rate limited; try again later")
        if isinstance(e, anthropic.APIStatusError):
            raise ScorerUnavailable(f"API error {e.status_code}: {e.message}")
        raise ScorerUnavailable("no network connection")  # APIConnectionError / timeout
    if response.stop_reason in ("refusal", "max_tokens"):
        raise ScorerUnavailable(f"model stopped with {response.stop_reason}")
    text = next((b.text for b in response.content if b.type == "text"), None)
    try:
        items = json.loads(text)["scores"]
    except (TypeError, ValueError, KeyError):
        raise ScorerUnavailable("unreadable reply")
    wanted, got = set(pairs), {}
    for it in items:
        p = tuple(sorted((it.get("a"), it.get("b"))))
        if p in wanted and isinstance(it.get("score"), int) and 0 <= it["score"] <= 10:
            got[p] = it["score"]
    return got


def claude_scores(conn, ids, client=None, log=None):
    """Cached Claude scores for every pair in `ids`; asks Claude only for pairs not cached."""
    log = log or (lambda m: print(m, file=sys.stderr))
    sayings = {i: records.get(conn, i) for i in ids}
    hashes = {i: fingerprint(s) for i, s in sayings.items()}
    scores, todo = {}, []
    for a, b in _pairs(ids):
        row = conn.execute("SELECT score FROM relatedness_cache WHERE scorer='claude' AND model=? AND a_id=? AND b_id=? "
                           "AND a_hash=? AND b_hash=?", (MODEL, a, b, hashes[a], hashes[b])).fetchone()
        if row:
            scores[(a, b)] = row[0]
        else:
            todo.append((a, b))
    if not todo:
        return scores
    client = client or _client()
    log(f"Asking {MODEL} to score {len(todo)} new pair(s) ({len(scores)} already cached)...")
    for start in range(0, len(todo), PAIRS_PER_REQUEST):
        chunk = todo[start:start + PAIRS_PER_REQUEST]
        involved = {i: sayings[i] for p in chunk for i in p}
        got = _ask_claude(client, involved, chunk)
        missing = [p for p in chunk if p not in got]
        if missing:  # one retry for pairs the model skipped
            got.update(_ask_claude(client, {i: sayings[i] for p in missing for i in p}, missing))
        still = [p for p in chunk if p not in got]
        if still:
            raise ScorerUnavailable(f"{len(still)} pair(s) were never scored")
        for (a, b), s in got.items():
            conn.execute("INSERT OR REPLACE INTO relatedness_cache (scorer, model, a_id, b_id, a_hash, b_hash, score) "
                         "VALUES ('claude', ?, ?, ?, ?, ?, ?)", (MODEL, a, b, hashes[a], hashes[b], s / 10))
            scores[(a, b)] = s / 10
        conn.commit()  # keep finished chunks even if a later one fails
        log(f"  scored {min(start + PAIRS_PER_REQUEST, len(todo))}/{len(todo)}")
    return scores


def uncached_count(conn, ids):
    hashes = {i: fingerprint(records.get(conn, i)) for i in ids}
    return sum(1 for a, b in _pairs(ids) if not conn.execute(
        "SELECT 1 FROM relatedness_cache WHERE scorer='claude' AND model=? AND a_id=? AND b_id=? AND a_hash=? AND b_hash=?",
        (MODEL, a, b, hashes[a], hashes[b])).fetchone())


SCORERS = {"claude": claude_scores, "words": word_scores}


def meaning_relatedness(conn, ids, scorer="claude", fallback=True, client=None, log=None):
    """Return (scores, info) where info records which scorer actually ran (for the run's params)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    if scorer == "claude":
        try:
            return claude_scores(conn, ids, client=client, log=log), {"scorer": "claude", "model": MODEL}
        except ScorerUnavailable as e:
            if not fallback:
                raise
            log(f"Note: Claude scoring unavailable ({e}); using the offline 'words' scorer instead.")
    return word_scores(conn, ids), {"scorer": "words"}


# ---------------------------------------------------------------- comparison

def _ranks(values):
    """Percentile rank (0..1) of each value, ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda k: values[k])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 / max(1, len(values) - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def compare(tag_scores, meaning_scores, ids, top=12):
    """Where tags and meaning disagree.

    hidden connections: meaning in the top 10% of pairs, but no shared tags.
    possible mis-tags:  at least 40% shared tags, but meaning in the bottom 25%.
    agreement: Spearman rank correlation of the two scores over all pairs.
    Ranks are used because the two scales aren't comparable.
    """
    pairs = _pairs(ids)
    t = [tag_scores.get(p, 0.0) for p in pairs]
    m = [meaning_scores.get(p, 0.0) for p in pairs]
    rt, rm = _ranks(t), _ranks(m)
    n = len(pairs)
    agreement = None
    if n > 2:
        mt, mm = sum(rt) / n, sum(rm) / n
        cov = sum((a - mt) * (b - mm) for a, b in zip(rt, rm))
        sd = math.sqrt(sum((a - mt) ** 2 for a in rt) * sum((b - mm) ** 2 for b in rm))
        agreement = round(cov / sd, 3) if sd else None
    hidden = sorted(((p, ms, ts) for p, ms, ts, r in zip(pairs, m, t, rm) if ts == 0 and r >= 0.9),
                    key=lambda x: -x[1])[:top]
    mistag = sorted(((p, ms, ts) for p, ms, ts, r in zip(pairs, m, t, rm) if ts >= 0.4 and r <= 0.25),
                    key=lambda x: -x[2])[:top]
    fmt = lambda rows: [{"a": a, "b": b, "meaning": round(ms, 3), "tags": round(ts, 3)} for (a, b), ms, ts in rows]
    return {"agreement": agreement, "pairs": n, "hidden": fmt(hidden), "mistag": fmt(mistag)}
