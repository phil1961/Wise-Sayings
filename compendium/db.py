"""Database connection, schema, and tag-vocabulary sync."""

import json
import sqlite3
from pathlib import Path

from . import CONFIDENCES, FORMS, STATUSES, TAG_KINDS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "sayings.db"
DEFAULT_VOCAB = ROOT / "tags.txt"


def _in(values):
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sayings (
    id                     INTEGER PRIMARY KEY,
    text                   TEXT NOT NULL UNIQUE,
    variants               TEXT NOT NULL DEFAULT '[]',   -- JSON list of strings
    form                   TEXT NOT NULL CHECK (form IN {_in(FORMS)}),
    attribution            TEXT,
    attribution_confidence TEXT NOT NULL CHECK (attribution_confidence IN {_in(CONFIDENCES)}),
    documented_in          TEXT NOT NULL CHECK (length(trim(documented_in)) > 0),
    lens                   TEXT NOT NULL,
    impact_score           INTEGER NOT NULL CHECK (impact_score BETWEEN 1 AND 10),
    impact_rationale       TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN {_in(STATUSES)}),
    review_note            TEXT,
    added_at               TEXT NOT NULL DEFAULT (datetime('now')),
    -- anonymous <=> no attribution
    CHECK ((attribution_confidence = 'anonymous') = (attribution IS NULL))
);

CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN {_in(TAG_KINDS)}),
    aliases TEXT NOT NULL DEFAULT '[]',                  -- JSON list, used by consult
    UNIQUE (name, kind)
);

CREATE TABLE IF NOT EXISTS saying_tags (
    saying_id INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (saying_id, tag_id)
);

-- Symmetric "pulls the opposite way" links, stored once with a_id < b_id.
CREATE TABLE IF NOT EXISTS counter_pairs (
    a_id INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
    b_id INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
    PRIMARY KEY (a_id, b_id),
    CHECK (a_id < b_id)
);
"""


# Schema changes after the original schema, applied in order and tracked with
# PRAGMA user_version. Never edit a shipped migration; add a new number instead.
# Version 1 is SCHEMA above.
MIGRATIONS = {
    2: """
    -- Key/value settings, e.g. default_status for new imports.
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- One row per sphere layout run (docs/sphere-layout-spec.md).
    CREATE TABLE layout_runs (
        id         INTEGER PRIMARY KEY,
        stage      TEXT NOT NULL,               -- 'tags' (stage one) or 'meaning' (stage two)
        seed       INTEGER NOT NULL,
        sample     TEXT NOT NULL,               -- JSON list of saying ids, in layout order
        sample_how TEXT NOT NULL,               -- how the sample was chosen, e.g. 'random 15' or 'ids'
        params     TEXT NOT NULL,               -- JSON: force weights, restarts, snap, etc.
        energy     REAL NOT NULL,
        stats      TEXT NOT NULL,               -- JSON quality measures (see sphere.layout_stats)
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- Where each saying ended up. x,y,z is the relaxed point on the unit sphere;
    -- vertex is the index of the frequency-10 lattice point it snapped to (NULL if not snapped).
    CREATE TABLE layout_points (
        run_id    INTEGER NOT NULL REFERENCES layout_runs(id) ON DELETE CASCADE,
        saying_id INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
        x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
        vertex    INTEGER,
        PRIMARY KEY (run_id, saying_id)
    );

    -- Edges of the spherical Delaunay triangulation (convex hull) of a run.
    CREATE TABLE layout_edges (
        run_id INTEGER NOT NULL REFERENCES layout_runs(id) ON DELETE CASCADE,
        a_id   INTEGER NOT NULL,
        b_id   INTEGER NOT NULL,
        PRIMARY KEY (run_id, a_id, b_id),
        CHECK (a_id < b_id)
    );
    """,
    3: """
    -- Stage two: cached meaning scores for a pair of sayings, per scorer and model.
    -- a_hash/b_hash fingerprint each saying's text+lens; a changed saying is re-scored.
    CREATE TABLE relatedness_cache (
        scorer     TEXT NOT NULL,               -- e.g. 'claude'
        model      TEXT NOT NULL,
        a_id       INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
        b_id       INTEGER NOT NULL REFERENCES sayings(id) ON DELETE CASCADE,
        a_hash     TEXT NOT NULL,
        b_hash     TEXT NOT NULL,
        score      REAL NOT NULL,               -- 0..1
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (scorer, model, a_id, b_id),
        CHECK (a_id < b_id)
    );

    -- The relatedness score each triangulation edge had in its run (drawn as line brightness).
    ALTER TABLE layout_edges ADD COLUMN relatedness REAL;

    -- A stage one vs stage two comparison on the same sample.
    CREATE TABLE comparisons (
        id          INTEGER PRIMARY KEY,
        tags_run    INTEGER NOT NULL REFERENCES layout_runs(id) ON DELETE CASCADE,
        meaning_run INTEGER NOT NULL REFERENCES layout_runs(id) ON DELETE CASCADE,
        report      TEXT NOT NULL,              -- JSON: agreement, hidden connections, possible mis-tags
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
}


def migrate(conn):
    """Bring the DB up to the latest schema version, keeping existing data."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:  # databases created before migrations existed already have SCHEMA
        version = 1
        conn.execute("PRAGMA user_version = 1")
    for n in sorted(MIGRATIONS):
        if n > version:
            with conn:
                conn.executescript(MIGRATIONS[n])
                conn.execute(f"PRAGMA user_version = {n}")
    return conn.execute("PRAGMA user_version").fetchone()[0]


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def parse_vocab(path):
    """Parse the tags vocabulary file into {(kind, name): [aliases]}."""
    vocab, kind = {}, None
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            kind = line[1:-1].strip().lower()
            if kind not in TAG_KINDS:
                raise ValueError(f"{path}:{lineno}: unknown section [{kind}]")
            continue
        if kind is None:
            raise ValueError(f"{path}:{lineno}: tag before any [subject]/[situation] section")
        name, _, alias_part = line.partition(":")
        name = name.strip().lower()
        if not name.replace("_", "").isalnum():
            raise ValueError(f"{path}:{lineno}: bad tag name {name!r} (use letters, digits, _)")
        aliases = [a.strip().lower() for a in alias_part.split(",") if a.strip()]
        vocab[(kind, name)] = aliases
    return vocab


def sync_vocab(conn, vocab_path):
    """Make the tags table match the vocabulary file.

    Tags removed from the file are deleted if unused; if still used they are
    kept and reported, so no saying silently loses a tag.
    """
    vocab = parse_vocab(vocab_path)
    for (kind, name), aliases in vocab.items():
        conn.execute(
            "INSERT INTO tags (name, kind, aliases) VALUES (?, ?, ?) "
            "ON CONFLICT (name, kind) DO UPDATE SET aliases = excluded.aliases",
            (name, kind, json.dumps(aliases)),
        )
    orphans = []
    for tag_id, name, kind in conn.execute("SELECT id, name, kind FROM tags").fetchall():
        if (kind, name) in vocab:
            continue
        used = conn.execute("SELECT 1 FROM saying_tags WHERE tag_id = ?", (tag_id,)).fetchone()
        if used:
            orphans.append(f"{kind}:{name}")
        else:
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()
    return orphans


def connect(db_path=DEFAULT_DB, vocab_path=DEFAULT_VOCAB):
    """Open the DB, apply the schema, and sync the tag vocabulary.

    Returns (conn, orphan_tags) where orphan_tags lists in-use tags that are
    no longer in the vocabulary file.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    orphans = sync_vocab(conn, vocab_path)
    return conn, orphans
