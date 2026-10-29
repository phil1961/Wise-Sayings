"""Meaning axes for the sayings sphere: rods through the globe's centre, a thesis at one end and its
antithesis at the opposite end (axes.txt). Each saying is rated on each axis from -2 (strongly the
antithesis) through 0 (not about this axis) to +2 (strongly the thesis), and the layout pulls it toward
the poles it embodies, so sayings with opposite meanings end up facing each other across the globe.

Directions are exactly equally spaced:
  1-3 axes  the x, y and z axes (90 degrees apart);
  4-6 axes  the icosahedron's corner pairs (63.4 degrees between neighbours), the same icosahedron the
            frequency-10 lattice is built from, so every pole is a lattice vertex.

Ratings come from Claude through claude_watcher, cached in axis_scores. There is no offline fallback:
without ratings, a layout simply has no axis pull (and says so).
"""

import hashlib
import json
import math
import sys
from pathlib import Path

from . import geodesic, records, watcher
from .meaning import MODEL, fingerprint

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "axes.txt"
SAYINGS_PER_CALL = 25
MAX_AXES = 6


class AxesUnavailable(Exception):
    pass


def load(path=DEFAULT_FILE):
    """[{key, thesis, antithesis, about, hash}] from the axes file, in file order."""
    out = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, rest = line.partition(":")
        parts = [p.strip() for p in rest.split("|")]
        key = key.strip().lower()
        if not sep or len(parts) < 2 or not parts[0] or not parts[1] or not key.replace("_", "").isalnum():
            raise ValueError(f"{path}:{lineno}: expected 'key: Thesis | Antithesis | what it is about'")
        about = parts[2] if len(parts) > 2 else ""
        h = hashlib.sha1(f"{parts[0]}|{parts[1]}|{about}".encode("utf-8")).hexdigest()[:12]
        out.append({"key": key, "thesis": parts[0], "antithesis": parts[1], "about": about, "hash": h})
    if len(out) > MAX_AXES:
        raise ValueError(f"{path}: at most {MAX_AXES} axes (there are only 6 equally spaced lines through a sphere's centre "
                         f"that also suit the lattice); found {len(out)}")
    if len({a["key"] for a in out}) != len(out):
        raise ValueError(f"{path}: axis keys must be unique")
    return out


def directions(n):
    """n unit vectors (thesis poles), equally spaced; the antithesis pole of each is its negative."""
    if n <= 0:
        return []
    if n <= 3:
        return [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)][:n]
    corners = [geodesic.normalize(c) for c in geodesic._CORNERS]
    picked = []
    for c in corners:  # one corner from each antipodal pair, in the lattice's own order
        if not any(abs(sum(a * b for a, b in zip(c, p)) + 1) < 1e-9 for p in picked):
            picked.append(c)
    return picked[:n]


# ---------------------------------------------------------------- ratings

def _schema(axes):
    return {
        "type": "object",
        "properties": {"ratings": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"},
                           **{a["key"]: {"type": "integer", "enum": [-2, -1, 0, 1, 2]} for a in axes}},
            "required": ["id"] + [a["key"] for a in axes],
            "additionalProperties": False}}},
        "required": ["ratings"],
        "additionalProperties": False,
    }


INSTRUCTIONS = """You rate sayings (proverbs, maxims, aphorisms, apophthegms) on meaning axes. Each axis has a THESIS
and its opposite, the ANTITHESIS. For every saying and every axis, give one integer:
  +2  the saying strongly advises or embodies the thesis
  +1  it leans toward the thesis
   0  it is not about this axis, or is balanced on it
  -1  it leans toward the antithesis
  -2  it strongly advises or embodies the antithesis
Judge the saying's meaning and advice (use its lens), not its surface words. Most sayings are about only one or
two axes: give 0 elsewhere, and keep 2 for sayings that are clearly about that axis. Rate every listed saying,
using its bracketed id."""


def _prompt(axes, sayings):
    axis_lines = "\n".join(f"- {a['key']}: THESIS \"{a['thesis']}\"  vs  ANTITHESIS \"{a['antithesis']}\""
                           + (f"  ({a['about']})" if a["about"] else "") for a in axes)
    listing = "\n".join(f"[{s['id']}] {s['text']}\n     Lens: {s['lens']}" for s in sayings)
    return f"{INSTRUCTIONS}\n\nAxes:\n{axis_lines}\n\nSayings:\n{listing}"


def missing(conn, ids, axes):
    """Sayings that still need a rating on at least one axis (new, edited, or the axis was edited)."""
    need = []
    for sid in ids:
        h = fingerprint(records.get(conn, sid))
        for a in axes:
            row = conn.execute("SELECT 1 FROM axis_scores WHERE axis_key=? AND saying_id=? AND model=? "
                               "AND axis_hash=? AND saying_hash=?", (a["key"], sid, MODEL, a["hash"], h)).fetchone()
            if not row:
                need.append(sid)
                break
    return need


def calls_needed(conn, ids, axes):
    return -(-len(missing(conn, ids, axes)) // SAYINGS_PER_CALL)


def scores(conn, ids, axes, log=None, ask=None):
    """{axis_key: {saying_id: -2..2}} for every saying and axis, asking Claude only for what isn't cached.

    `ask` defaults to watcher.ask (tests pass a fake). Raises AxesUnavailable if ratings can't be had.
    """
    log = log or (lambda m: print(m, file=sys.stderr))
    ask = ask or watcher.ask
    todo = missing(conn, ids, axes)
    if todo:
        log(f"Asking {MODEL} via claude_watcher to rate {len(todo)} saying(s) on {len(axes)} axes "
            f"({-(-len(todo) // SAYINGS_PER_CALL)} call(s))...")
        for start in range(0, len(todo), SAYINGS_PER_CALL):
            chunk = [records.get(conn, sid) for sid in todo[start:start + SAYINGS_PER_CALL]]
            try:
                raw, _ = ask(_prompt(axes, chunk), MODEL, schema=_schema(axes), log=log)
            except watcher.WatcherUnavailable as e:
                raise AxesUnavailable(str(e))
            got = {r.get("id"): r for r in (raw or {}).get("ratings", []) if isinstance(r, dict)}
            for s in chunk:
                r = got.get(s["id"])
                if r is None:
                    raise AxesUnavailable(f"no rating came back for saying #{s['id']}")
                for a in axes:
                    v = r.get(a["key"])
                    if v not in (-2, -1, 0, 1, 2):
                        raise AxesUnavailable(f"bad rating for #{s['id']} on {a['key']}: {v!r}")
                    conn.execute("INSERT OR REPLACE INTO axis_scores (axis_key, axis_hash, saying_id, saying_hash, model, score) "
                                 "VALUES (?,?,?,?,?,?)", (a["key"], a["hash"], s["id"], fingerprint(s), MODEL, v))
            conn.commit()  # keep finished chunks even if a later one fails
            log(f"  rated {min(start + SAYINGS_PER_CALL, len(todo))}/{len(todo)}")
    out = {a["key"]: {} for a in axes}
    for a in axes:
        for sid in ids:
            row = conn.execute("SELECT score FROM axis_scores WHERE axis_key=? AND saying_id=? AND model=? AND axis_hash=?",
                               (a["key"], sid, MODEL, a["hash"])).fetchone()
            out[a["key"]][sid] = row[0] if row else 0
    return out


def for_layout(axes, ratings):
    """What run_layout needs: [{key, thesis, antithesis, dir, scores: {id: -2..2}}]."""
    return [{"key": a["key"], "thesis": a["thesis"], "antithesis": a["antithesis"], "dir": list(d),
             "scores": ratings[a["key"]]} for a, d in zip(axes, directions(len(axes)))]
