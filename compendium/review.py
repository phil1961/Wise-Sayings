"""Markdown review batches: export candidates, then apply accept/reject decisions."""

import re
from datetime import datetime
from pathlib import Path

from . import records

HEADER = """# Review batch: {n} candidate(s)

Generated {when}. For each saying, fill in the `Decision:` line with `accept` or
`reject`. You can add a note after a `|`, e.g. `Decision: reject | redundant with #4`.
Leave a line blank to skip that saying for now. Then run:

    python sayings.py review apply {path}

"""


def _quote(conn, sid):
    row = conn.execute("SELECT text FROM sayings WHERE id=?", (sid,)).fetchone()
    return f'#{sid} "{row["text"]}"' if row else f"#{sid}"


def export_candidates(conn, out_path, status="candidate"):
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sayings WHERE status=? ORDER BY id", (status,))]
    out_path = Path(out_path)
    parts = [HEADER.format(n=len(ids), when=datetime.now().strftime("%Y-%m-%d %H:%M"), path=out_path)]
    for sid in ids:
        s = records.get(conn, sid)
        who = s["attribution"] or "anonymous"
        lines = [
            "---", "",
            f"## #{sid} · {s['form']}", "",
            f"> {s['text']}", "",
        ]
        if s["variants"]:
            lines.append(f"- **Variants:** {' / '.join(s['variants'])}")
        lines += [
            f"- **Attribution:** {who} ({s['attribution_confidence']})",
            f"- **Documented in:** {s['documented_in']}",
            f"- **Lens:** {s['lens']}",
            f"- **Impact:** {s['impact_score']}/10: {s['impact_rationale']}",
            f"- **Subject tags:** {', '.join(s['subject_tags']) or '(none)'}",
            f"- **Situation tags:** {', '.join(s['situation_tags'])}",
        ]
        if s["counter_sayings"]:
            lines.append(f"- **Counter-sayings:** {'; '.join(_quote(conn, c) for c in s['counter_sayings'])}")
        for other, text, score in records.near_duplicates(conn, s["text"], exclude_id=sid):
            lines.append(f'- **⚠ Possible near-duplicate:** #{other} "{text}" (similarity {score})')
        lines += ["", "Decision: ", ""]
        parts.append("\n".join(lines))
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return len(ids)


def parse_decisions(text):
    """Return [(id, decision, note)] for every filled-in Decision line."""
    decisions, current = [], None
    for line in text.splitlines():
        m = re.match(r"^##\s+#(\d+)\b", line)
        if m:
            current = int(m.group(1))
            continue
        m = re.match(r"^Decision:\s*(.*)$", line, re.IGNORECASE)
        if m and current is not None:
            value, _, note = m.group(1).partition("|")
            value = value.strip().lower()
            if value:
                decisions.append((current, value, note.strip() or None))
            current = None
    return decisions


def apply_decisions(conn, path):
    mapping = {"accept": "accepted", "accepted": "accepted", "reject": "rejected", "rejected": "rejected"}
    applied, problems = {"accepted": 0, "rejected": 0}, []
    for sid, value, note in parse_decisions(Path(path).read_text(encoding="utf-8")):
        status = mapping.get(value)
        if status is None:
            problems.append(f"#{sid}: unrecognized decision {value!r} (use accept or reject)")
            continue
        cur = conn.execute("UPDATE sayings SET status=?, review_note=COALESCE(?, review_note) WHERE id=?",
                           (status, note, sid))   # no note on the line keeps the old one, as set-status does
        if cur.rowcount == 0:
            problems.append(f"#{sid}: no such saying")
            continue
        applied[status] += 1
    conn.commit()
    return applied, problems
