"""Text formatting, full-compendium export, and stats."""

import json
import textwrap
from pathlib import Path

from . import records


def attribution_line(s):
    if s["attribution_confidence"] == "anonymous":
        return "anonymous"
    note = {"verified": "", "attributed_uncertain": " (attribution uncertain)",
            "misattributed": " (MISATTRIBUTED)"}[s["attribution_confidence"]]
    return f"{s['attribution']}{note}"


def wrap(text, indent="    "):
    return textwrap.fill(text, width=88, initial_indent=indent, subsequent_indent=indent)


def format_saying(s, verbose=False):
    lines = [f'  #{s["id"]}  "{s["text"]}"',
             f"      {attribution_line(s)} · {s['form']} · impact {s['impact_score']}"
             + (f" · {s['status']}" if s["status"] != "accepted" else "")]
    lines.append(wrap("Lens: " + s["lens"], "      "))
    if verbose:
        lines.append(wrap("Documented in: " + s["documented_in"], "      "))
        lines.append(wrap("Why this score: " + s["impact_rationale"], "      "))
        lines.append(f"      Tags: {', '.join(s['subject_tags'] + s['situation_tags'])}")
        if s["variants"]:
            lines.append(wrap("Variants: " + " / ".join(s["variants"]), "      "))
        if s["counter_sayings"]:
            lines.append("      Counter-sayings: " + ", ".join(f"#{c}" for c in s["counter_sayings"]))
    return "\n".join(lines)


def all_ids(conn, status="accepted"):
    sql = "SELECT id FROM sayings" + ("" if status == "all" else " WHERE status=?")
    sql += " ORDER BY id"
    return [r["id"] for r in conn.execute(sql, () if status == "all" else (status,))]


def export_json(conn, path, status="accepted"):
    data = [records.get(conn, i) for i in all_ids(conn, status)]
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(data)


def export_markdown(conn, path, status="accepted"):
    ids = all_ids(conn, status)
    by_form = {}
    for i in ids:
        s = records.get(conn, i)
        by_form.setdefault(s["form"], []).append(s)
    out = [f"# Sayings Compendium\n\n{len(ids)} saying(s), status: {status}.\n"]
    for form in sorted(by_form):
        out.append(f"\n## {form.capitalize()}s\n")
        for s in sorted(by_form[form], key=lambda s: (-s["impact_score"], s["id"])):
            out.append(f"### #{s['id']}: {s['text']}\n")
            out.append(f"- **Attribution:** {attribution_line(s)}")
            if s["variants"]:
                out.append(f"- **Variants:** {' / '.join(s['variants'])}")
            out.append(f"- **Documented in:** {s['documented_in']}")
            out.append(f"- **Lens:** {s['lens']}")
            out.append(f"- **Impact:** {s['impact_score']}/10: {s['impact_rationale']}")
            out.append(f"- **Tags:** {', '.join(s['subject_tags'])} | {', '.join(s['situation_tags'])}")
            if s["counter_sayings"]:
                out.append(f"- **Counter-sayings:** {', '.join(f'#{c}' for c in s['counter_sayings'])}")
            out.append("")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return len(ids)


def stats(conn, status="accepted"):
    """Counts by status (always all rows), and by form/confidence/tag for `status`."""
    where, args = ("", ()) if status == "all" else ("WHERE s.status = ?", (status,))
    q = lambda sql, a=args: [tuple(r) for r in conn.execute(sql, a)]
    return {
        "status": q("SELECT status, COUNT(*) FROM sayings GROUP BY status ORDER BY 2 DESC", ()),
        "form": q(f"SELECT form, COUNT(*) FROM sayings s {where} GROUP BY form ORDER BY 2 DESC"),
        "confidence": q(f"SELECT attribution_confidence, COUNT(*) FROM sayings s {where} "
                        "GROUP BY 1 ORDER BY 2 DESC"),
        "tag": q("SELECT t.kind || ':' || t.name, COUNT(*) FROM tags t JOIN saying_tags st ON st.tag_id=t.id "
                 f"JOIN sayings s ON s.id=st.saying_id {where} GROUP BY t.id ORDER BY 2 DESC, 1"),
        "unused_tags": q("SELECT kind || ':' || name FROM tags WHERE id NOT IN (SELECT tag_id FROM saying_tags) "
                         "ORDER BY 1", ()),
    }
