"""Command-line interface. Run via `python sayings.py <command> ...`."""

import argparse
import sys
from pathlib import Path

from . import CONFIDENCES, FORMS, STATUSES, consult as consult_mod, db, records, report, review


def _print_import_result(res):
    if res["errors"]:
        print("Import aborted; nothing was added. Fix these and re-run:")
        for e in res["errors"]:
            print("  -", e)
        return 1
    print(f"Added {len(res['added'])} saying(s) as candidates: "
          + ", ".join(f"#{i}" for i in res["added"]))
    for new, old, score in res["duplicates"]:
        print(f"  ⚠ #{new} looks like a near-duplicate of #{old} (similarity {score})")
    if res["added"]:
        print("Next: python sayings.py review export")
    return 0


def cmd_import(conn, a):
    return _print_import_result(records.import_records(conn, records.load_file(a.file)))


def cmd_add(conn, a):
    raw = {
        "text": a.text, "form": a.form, "attribution": a.by,
        "attribution_confidence": a.confidence, "documented_in": a.documented_in,
        "lens": a.lens, "impact_score": a.impact, "impact_rationale": a.rationale,
        "variants": a.variant, "subject_tags": a.subject, "situation_tags": a.situation,
        "counter_sayings": a.counter,
    }
    return _print_import_result(records.import_records(conn, [raw]))


def cmd_review(conn, a):
    if a.action == "export":
        n = review.export_candidates(conn, a.file, status=a.status)
        print(f"Wrote {n} saying(s) to {a.file}. Fill in the Decision lines, then:")
        print(f"  python sayings.py review apply {a.file}")
    else:
        applied, problems = review.apply_decisions(conn, a.file)
        print(f"Accepted {applied['accepted']}, rejected {applied['rejected']}.")
        for p in problems:
            print("  -", p)
        return 1 if problems else 0


def cmd_search(conn, a):
    found = records.search(conn, a.text, a.tag, a.form, a.by, a.status)
    if not found:
        print("No matches.")
    for s in found:
        print(report.format_saying(s, verbose=a.verbose) + "\n")
    print(f"{len(found)} result(s).")


def cmd_show(conn, a):
    s = records.get(conn, a.id)
    if not s:
        print(f"No saying #{a.id}.")
        return 1
    print(report.format_saying(s, verbose=True))


def cmd_consult(conn, a):
    matcher = consult_mod.load_matcher(a.matcher)
    groups, tensions, matched = consult_mod.consult(conn, a.situation, a.limit, matcher)
    if not groups:
        print("No accepted sayings matched. Try different words, or see `python sayings.py tags`.")
        if not conn.execute("SELECT 1 FROM sayings WHERE status='accepted'").fetchone():
            print("(No sayings are accepted yet. Review the candidates first.)")
        return 0
    print(f"Situation tags detected: {', '.join(matched) or '(none; matched on wording)'}\n")
    for name, sayings in groups:
        print(f"== {name.replace('_', ' ')} ==")
        for s in sayings:
            print(report.format_saying(s) + "\n")
    if tensions:
        print("== Tensions (sayings that pull opposite ways) ==")
        for x, y, both in tensions:
            print(f'  #{x["id"]} "{x["text"]}"\n    vs\n  #{y["id"]} "{y["text"]}"')
            if not both:
                print("    (counter-saying pulled in from outside the top results)")
            print()


def cmd_export(conn, a):
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if a.format in ("md", "both"):
        n = report.export_markdown(conn, out / "compendium.md", a.status)
        print(f"Wrote {n} saying(s) to {out / 'compendium.md'}")
    if a.format in ("json", "both"):
        n = report.export_json(conn, out / "compendium.json", a.status)
        print(f"Wrote {n} saying(s) to {out / 'compendium.json'}")


def cmd_stats(conn, a):
    st = report.stats(conn, a.status)
    sections = [("By status (all records)", "status"), (f"By form ({a.status})", "form"),
                (f"By attribution confidence ({a.status})", "confidence"), (f"By tag ({a.status})", "tag")]
    for title, key in sections:
        print(title)
        for name, n in st[key] or [("(none)", 0)]:
            print(f"  {n:>4}  {name}")
        print()
    if st["unused_tags"]:
        print("Tags in the vocabulary not yet used: " + ", ".join(t for (t,) in st["unused_tags"]))


def cmd_tags(conn, a):
    for kind in ("situation", "subject"):
        print(f"[{kind}]")
        for r in conn.execute("SELECT name, aliases FROM tags WHERE kind=? ORDER BY name", (kind,)):
            print(f"  {r['name']}")
        print()
    print("Edit tags.txt to change the vocabulary; it is re-read on every command.")


def cmd_dedupe(conn, a):
    pairs = records.all_near_duplicates(conn, a.threshold)
    if not pairs:
        print("No near-duplicates found.")
    for x, y, score in pairs:
        tx = conn.execute("SELECT text FROM sayings WHERE id=?", (x,)).fetchone()["text"]
        ty = conn.execute("SELECT text FROM sayings WHERE id=?", (y,)).fetchone()["text"]
        print(f'{score:.2f}  #{x} "{tx}"\n      #{y} "{ty}"\n')


def build_parser():
    p = argparse.ArgumentParser(prog="sayings.py", description="Sayings Compendium Engine")
    p.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite file (default: sayings.db)")
    p.add_argument("--vocab", default=str(db.DEFAULT_VOCAB), help="tag vocabulary (default: tags.txt)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import", help="import a JSON or CSV batch as candidates")
    s.add_argument("file")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("add", help="add one saying as a candidate")
    s.add_argument("--text", required=True)
    s.add_argument("--form", required=True, choices=FORMS)
    s.add_argument("--by", help="attribution (omit if anonymous)")
    s.add_argument("--confidence", required=True, choices=CONFIDENCES)
    s.add_argument("--documented-in", required=True, help="where the saying is documented")
    s.add_argument("--lens", required=True)
    s.add_argument("--impact", required=True, type=int)
    s.add_argument("--rationale", required=True)
    s.add_argument("--variant", action="append", default=[])
    s.add_argument("--subject", action="append", default=[], help="subject tag (repeatable)")
    s.add_argument("--situation", action="append", default=[], help="situation tag (repeatable)")
    s.add_argument("--counter", action="append", default=[], help="counter saying id or exact text")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("review", help="export candidates for review, or apply decisions")
    s.add_argument("action", choices=("export", "apply"))
    s.add_argument("file", nargs="?", default="review.md")
    s.add_argument("--status", default="candidate", choices=STATUSES,
                   help="which records to export (default: candidate)")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("search", help="search by text, tag, form, attribution")
    s.add_argument("text", nargs="?")
    s.add_argument("--tag")
    s.add_argument("--form", choices=FORMS)
    s.add_argument("--by", help="attribution contains")
    s.add_argument("--status", default="accepted", choices=STATUSES + ("all",))
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("show", help="show one saying in full")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("consult", help="get relevant sayings for a situation")
    s.add_argument("situation")
    s.add_argument("--limit", type=int, default=12)
    s.add_argument("--matcher", help="module:function to replace the keyword matcher")
    s.set_defaults(func=cmd_consult)

    s = sub.add_parser("export", help="export the compendium to markdown and/or JSON")
    s.add_argument("--format", choices=("md", "json", "both"), default="both")
    s.add_argument("--out-dir", default="export")
    s.add_argument("--status", default="accepted", choices=STATUSES + ("all",))
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="counts by form, tag, confidence")
    s.add_argument("--status", default="accepted", choices=STATUSES + ("all",))
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("tags", help="list the tag vocabulary")
    s.set_defaults(func=cmd_tags)

    s = sub.add_parser("dedupe", help="list near-duplicate pairs")
    s.add_argument("--threshold", type=float, default=0.6)
    s.set_defaults(func=cmd_dedupe)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        conn, orphans = db.connect(args.db, args.vocab)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    for t in orphans:
        print(f"Note: tag {t} was removed from the vocabulary but is still in use.", file=sys.stderr)
    try:
        return args.func(conn, args) or 0
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
