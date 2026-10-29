"""Validate, insert, look up, dedupe, and search sayings."""

import csv
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from . import CONFIDENCES, FORMS, STATUSES

REQUIRED = ("text", "form", "attribution_confidence", "documented_in",
            "lens", "impact_score", "impact_rationale")
LIST_FIELDS = ("variants", "subject_tags", "situation_tags", "counter_sayings")
KNOWN = set(REQUIRED) | set(LIST_FIELDS) | {"attribution", "status"}

STOPWORDS = set("""
a an the and or but if of to in on at by for with from as is are was were be been being
it its this that these those i you he she we they me him her us them my your his our their
not no nor so than then there here what which who whom whose when where why how all any
each some such can could will would shall should may might must do does did done have has had
one ones just very more most much many also into out up down over under again only own same
""".split())


# ---------------------------------------------------------------- validation

def _as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    if isinstance(value, (int, float)):
        return [value]
    return [v.strip() for v in str(value).split(";") if v.strip()]


def validate(raw, conn):
    """Return (normalized_record, errors). Tags are checked against the DB vocabulary."""
    errors = []
    unknown = set(raw) - KNOWN - {"id", "added_at"}
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")

    rec = {k: raw.get(k) for k in KNOWN}
    for k in REQUIRED:
        if rec[k] is None or str(rec[k]).strip() == "":
            errors.append(f"missing {k}")
    for k in ("text", "form", "attribution", "attribution_confidence",
              "documented_in", "lens", "impact_rationale", "status"):
        if isinstance(rec[k], str):
            rec[k] = rec[k].strip() or None
    for k in LIST_FIELDS:
        rec[k] = _as_list(raw.get(k))

    if rec["form"] and rec["form"] not in FORMS:
        errors.append(f"form must be one of {FORMS}")
    conf = rec["attribution_confidence"]
    if conf and conf not in CONFIDENCES:
        errors.append(f"attribution_confidence must be one of {CONFIDENCES}")
    if conf == "anonymous" and rec["attribution"]:
        errors.append("anonymous sayings must have no attribution")
    if conf and conf != "anonymous" and not rec["attribution"]:
        errors.append(f"attribution required when confidence is {conf}")
    rec["status"] = rec["status"] or default_status(conn)
    if rec["status"] not in STATUSES:
        errors.append(f"status must be one of {STATUSES}")
    try:
        rec["impact_score"] = int(rec["impact_score"])
        if not 1 <= rec["impact_score"] <= 10:
            errors.append("impact_score must be 1-10")
    except (TypeError, ValueError):
        if rec["impact_score"] is not None:
            errors.append("impact_score must be an integer 1-10")

    for kind in ("subject", "situation"):
        tags = [str(t).strip().lower() for t in rec[f"{kind}_tags"]]
        rec[f"{kind}_tags"] = tags
        for t in tags:
            if not conn.execute("SELECT 1 FROM tags WHERE kind=? AND name=?", (kind, t)).fetchone():
                errors.append(f"{kind} tag {t!r} is not in the vocabulary (tags.txt)")
    if not rec["situation_tags"]:
        errors.append("at least one situation tag is required (consult depends on it)")
    return rec, errors


def default_status(conn):
    """Status given to imports that don't set one (settings table; 'candidate' if unset)."""
    from .db import get_setting
    return get_setting(conn, "default_status", "candidate")


# ---------------------------------------------------------------- writing

def set_status(conn, ids, status, note=None):
    """Change the status of existing sayings. Returns the ids that didn't exist."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    missing = []
    for sid in ids:
        cur = conn.execute("UPDATE sayings SET status=?, review_note=COALESCE(?, review_note) WHERE id=?",
                           (status, note, sid))
        if cur.rowcount == 0:
            missing.append(sid)
    conn.commit()
    return missing


def _insert(conn, rec):
    cur = conn.execute(
        """INSERT INTO sayings (text, variants, form, attribution, attribution_confidence,
               documented_in, lens, impact_score, impact_rationale, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (rec["text"], json.dumps(rec["variants"], ensure_ascii=False), rec["form"],
         rec["attribution"], rec["attribution_confidence"], rec["documented_in"],
         rec["lens"], rec["impact_score"], rec["impact_rationale"], rec["status"]),
    )
    sid = cur.lastrowid
    for kind in ("subject", "situation"):
        for t in rec[f"{kind}_tags"]:
            conn.execute(
                "INSERT OR IGNORE INTO saying_tags (saying_id, tag_id) "
                "SELECT ?, id FROM tags WHERE kind=? AND name=?", (sid, kind, t))
    return sid


def link_counters(conn, a, b):
    if a == b:
        return
    a, b = sorted((a, b))
    conn.execute("INSERT OR IGNORE INTO counter_pairs (a_id, b_id) VALUES (?, ?)", (a, b))


def _resolve_ref(conn, ref):
    """A counter reference is an id (int or '#12') or the exact text of a saying."""
    s = str(ref).strip()
    if re.fullmatch(r"#?\d+", s):
        row = conn.execute("SELECT id FROM sayings WHERE id=?", (int(s.lstrip("#")),)).fetchone()
    else:
        row = conn.execute("SELECT id FROM sayings WHERE lower(text)=lower(?)", (s,)).fetchone()
    return row["id"] if row else None


def import_records(conn, raws):
    """Validate the whole batch first; insert all or nothing.

    Returns {"added": [ids], "errors": [str], "duplicates": [(new_id, old_id, score)]}.
    """
    batch_texts = set()
    validated, errors = [], []
    for i, raw in enumerate(raws, 1):
        rec, errs = validate(raw, conn)
        label = f"record {i} ({(rec.get('text') or '?')[:40]!r})"
        key = (rec.get("text") or "").lower()
        if key and (key in batch_texts or conn.execute(
                "SELECT 1 FROM sayings WHERE lower(text)=?", (key,)).fetchone()):
            errs.append("exact text already exists")
        batch_texts.add(key)
        errors += [f"{label}: {e}" for e in errs]
        validated.append(rec)
    if errors:
        return {"added": [], "errors": errors, "duplicates": []}

    try:
        ids = [_insert(conn, rec) for rec in validated]
        for sid, rec in zip(ids, validated):
            for ref in rec["counter_sayings"]:
                other = _resolve_ref(conn, ref)
                if other is None:
                    raise ValueError(f"{rec['text'][:40]!r}: counter saying {ref!r} not found")
                link_counters(conn, sid, other)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    dups = []
    for sid in ids:
        text = conn.execute("SELECT text FROM sayings WHERE id=?", (sid,)).fetchone()["text"]
        for other_id, _, score in near_duplicates(conn, text, exclude_id=sid):
            if other_id not in ids or other_id < sid:
                dups.append((sid, other_id, score))
    return {"added": ids, "errors": [], "duplicates": dups}


def load_file(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            return [{k: v for k, v in row.items() if v not in (None, "")} for row in csv.DictReader(f)]
    raise ValueError("import file must be .json or .csv")


# ---------------------------------------------------------------- reading

def get(conn, sid):
    row = conn.execute("SELECT * FROM sayings WHERE id=?", (sid,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["variants"] = json.loads(rec["variants"])
    for kind in ("subject", "situation"):
        rec[f"{kind}_tags"] = [r["name"] for r in conn.execute(
            "SELECT t.name FROM tags t JOIN saying_tags st ON st.tag_id=t.id "
            "WHERE st.saying_id=? AND t.kind=? ORDER BY t.name", (sid, kind))]
    rec["counter_sayings"] = counters_of(conn, sid)
    return rec


def counters_of(conn, sid):
    return [r[0] for r in conn.execute(
        "SELECT b_id FROM counter_pairs WHERE a_id=? UNION SELECT a_id FROM counter_pairs WHERE b_id=? "
        "ORDER BY 1", (sid, sid))]


def search(conn, text=None, tag=None, form=None, by=None, status="accepted"):
    sql, args = "SELECT DISTINCT s.id FROM sayings s", []
    where = []
    if tag:
        sql += " JOIN saying_tags st ON st.saying_id=s.id JOIN tags t ON t.id=st.tag_id"
        where.append("t.name = ?")
        args.append(tag.lower())
    if text:
        where.append("(s.text LIKE ? OR s.variants LIKE ? OR s.lens LIKE ?)")
        args += [f"%{text}%"] * 3
    if form:
        where.append("s.form = ?")
        args.append(form)
    if by:
        where.append("s.attribution LIKE ?")
        args.append(f"%{by}%")
    if status != "all":
        where.append("s.status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.impact_score DESC, s.id"
    return [get(conn, r["id"]) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- dedupe

def content_words(text):
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def similarity(a, b):
    """Max of character-level ratio and content-word overlap (Jaccard)."""
    ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
    wa, wb = content_words(a), content_words(b)
    jacc = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return max(ratio, jacc)


def near_duplicates(conn, text, exclude_id=None, threshold=0.6):
    """Flag sayings whose wording is close to `text` (not rejected ones)."""
    hits = []
    for r in conn.execute("SELECT id, text FROM sayings WHERE status != 'rejected'"):
        if r["id"] == exclude_id:
            continue
        score = similarity(text, r["text"])
        if score >= threshold:
            hits.append((r["id"], r["text"], round(score, 2)))
    return sorted(hits, key=lambda h: -h[2])


def all_near_duplicates(conn, threshold=0.6):
    rows = conn.execute("SELECT id, text FROM sayings WHERE status != 'rejected' ORDER BY id").fetchall()
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            score = similarity(a["text"], b["text"])
            if score >= threshold:
                pairs.append((a["id"], b["id"], round(score, 2)))
    return sorted(pairs, key=lambda p: -p[2])
