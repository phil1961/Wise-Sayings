"""Command-line interface. Run via `python sayings.py <command> ...`."""

import argparse
import json
import random
import sys
import webbrowser
from pathlib import Path

from . import CONFIDENCES, FORMS, STATUSES, consult as consult_mod, db, records, report, review


def _print_import_result(res):
    if res["errors"]:
        print("Import aborted; nothing was added. Fix these and re-run:")
        for e in res["errors"]:
            print("  -", e)
        return 1
    statuses = sorted({s for (s,) in res.get("statuses", [])}) or ["?"]
    print(f"Added {len(res['added'])} saying(s) as {'/'.join(statuses)}: "
          + ", ".join(f"#{i}" for i in res["added"]))
    for new, old, score in res["duplicates"]:
        print(f"  ⚠ #{new} looks like a near-duplicate of #{old} (similarity {score})")
    if "candidate" in statuses:
        print("Next: python sayings.py review export")
    return 0


def _import(conn, raws):
    res = records.import_records(conn, raws)
    if res["added"]:
        res["statuses"] = conn.execute(
            f"SELECT DISTINCT status FROM sayings WHERE id IN ({','.join('?' * len(res['added']))})",
            res["added"]).fetchall()
    return _print_import_result(res)


def cmd_import(conn, a):
    return _import(conn, records.load_file(a.file))


def cmd_add(conn, a):
    raw = {
        "text": a.text, "form": a.form, "attribution": a.by,
        "attribution_confidence": a.confidence, "documented_in": a.documented_in,
        "lens": a.lens, "impact_score": a.impact, "impact_rationale": a.rationale,
        "variants": a.variant, "subject_tags": a.subject, "situation_tags": a.situation,
        "counter_sayings": a.counter,
    }
    return _import(conn, [raw])


def cmd_set_status(conn, a):
    missing = records.set_status(conn, a.ids, a.status, a.note)
    done = [i for i in a.ids if i not in missing]
    if done:
        print(f"Set {len(done)} saying(s) to {a.status}: " + ", ".join(f"#{i}" for i in done))
    for i in missing:
        print(f"  - no saying #{i}")
    return 1 if missing else 0


def cmd_settings(conn, a):
    if a.key is None:
        print(f"default_status = {records.default_status(conn)}  (status for new imports)")
        for k, v in conn.execute("SELECT key, value FROM settings WHERE key != 'default_status' ORDER BY key"):
            print(f"{k} = {v}")
        return
    if a.key != "default_status":
        print("Known settings: default_status")
        return 1
    if a.value is None:
        print(f"default_status = {records.default_status(conn)}")
        return
    if a.value not in STATUSES:
        print(f"default_status must be one of {STATUSES}")
        return 1
    db.set_setting(conn, "default_status", a.value)
    print(f"New imports will now be '{a.value}'.")


def cmd_sphere(conn, a):
    from . import render, sphere
    if a.action == "list":
        rows = conn.execute("SELECT id, stage, seed, sample, sample_how, stats, created_at FROM layout_runs ORDER BY id").fetchall()
        if not rows:
            print("No layout runs yet. Try: python sayings.py sphere run --n 15")
        for r in rows:
            st = json.loads(r["stats"])
            print(f"#{r['id']}  {r['created_at']}  stage={r['stage']}  n={len(json.loads(r['sample']))}  "
                  f"seed={r['seed']}  ({r['sample_how']})  relatedness~angle={st.get('relatedness_vs_angle')}")
        return
    if a.action == "render":
        if a.run is None:
            print("Give a run id: python sayings.py sphere render 3")
            return 1
        out = render.write_page(conn, a.run, a.out or Path("export") / f"sphere-{a.run}.html")
        print(f"Wrote {out}")
        return _maybe_open(out, a.open)

    if a.action == "score":
        return _sphere_score(conn, a)
    if a.action == "compare":
        return _sphere_compare(conn, a)

    # run
    seed, ids, how = _sphere_sample(conn, a)
    if a.stage == "meaning" and not _cost_ok(conn, ids, a):
        return 1
    print(f"Laying out {len(ids)} sayings (stage {a.stage}, seed {seed})...")
    run_id = _layout(conn, a, ids, a.stage, seed, how)
    out = render.write_page(conn, run_id, a.out or Path("export") / f"sphere-{run_id}.html")
    print(f"Wrote {out}")
    return _maybe_open(out, a.open)


def _sphere_sample(conn, a):
    from . import sphere
    seed = a.seed if a.seed is not None else random.randrange(1_000_000)
    if a.ids:
        return seed, sorted({int(x) for x in a.ids.split(",")}), "ids"
    if getattr(a, "all", False):
        ids = [r[0] for r in conn.execute("SELECT id FROM sayings WHERE status='accepted' ORDER BY id")]
        return seed, ids, "all accepted"
    ids = sphere.choose_sample(conn, a.n, seed, a.include_candidates)
    return seed, ids, f"random {a.n}" + (" incl. candidates" if a.include_candidates else "")


LEAVE_FOR_OTHERS = 50   # watcher calls a bulk run here must leave for other apps today


def _cost_ok(conn, ids, a):
    """Claude calls go through claude_watcher, whose 200 calls a day (one at a time) are shared with
    other apps; ask for --yes before a run that needs more than a handful of them."""
    from . import meaning
    if a.scorer != "claude":
        return True
    new = meaning.uncached_count(conn, ids)
    calls = -(-new // meaning.PAIRS_PER_REQUEST)
    if calls > meaning.CALLS_WITHOUT_ASKING:
        from . import watcher
        u = watcher.usage() if meaning.TRANSPORT == "watcher" else None
        if u and calls > u.get("remaining", 0) - LEAVE_FOR_OTHERS:
            print(f"That needs about {calls} Claude calls, but the watcher has {u.get('remaining')} left today "
                  f"({u.get('used')} of {u.get('limit')} used), and {LEAVE_FOR_OTHERS} are kept for other apps. "
                  f"Try a smaller --n, or again after the UTC day turns over.")
            return False
    if calls > meaning.CALLS_WITHOUT_ASKING and not a.yes:
        print(f"That needs {new} new pair scores, about {calls} Claude calls. They go through claude_watcher, which "
              f"allows 200 calls a day shared with Network Meaning and runs one at a time, so this would hold it "
              f"for a while. Re-run with --yes at a quiet time, use a smaller --n, or --scorer words (offline).")
        return False
    return True


def _layout(conn, a, ids, stage, seed, how, related=None, info=None):
    from . import sphere
    if stage == "meaning" and related is None:
        related, info = sphere.stage_relatedness(conn, ids, "meaning", scorer=a.scorer)
    run_id = sphere.run_layout(conn, ids, stage=stage, seed=seed, sample_how=how, related=related, stage_info=info,
                               pull=a.pull, push_counters=a.push_counters, form_weight=a.form_weight,
                               restarts=a.restarts, snap=not a.no_snap)
    run = sphere.load_run(conn, run_id)
    st = run["stats"]
    from .render import stage_label
    print(f"Run #{run_id} saved ({stage_label(run)}).")
    print(f"  related pairs sit {st['related_mean_angle']}° apart on average, unrelated {st['unrelated_mean_angle']}°"
          f" (correlation {st['relatedness_vs_angle']})")
    print(f"  closest pair {st['min_angle']}° apart" + (f"; snapping moved points up to {st['max_snap_shift']}°" if 'max_snap_shift' in st else ""))
    if st["counter_angles"]:
        print(f"  counter-sayings apart: {', '.join(f'{x}°' for x in st['counter_angles'])} (180° = opposite sides)")
    return run_id


def _sphere_score(conn, a):
    from . import meaning
    seed, ids, how = _sphere_sample(conn, a)
    new = meaning.uncached_count(conn, ids)
    print(f"{len(ids)} sayings ({how}): {len(ids) * (len(ids) - 1) // 2} pairs, {new} not yet scored by {meaning.MODEL}.")
    if not new or not _cost_ok(conn, ids, a):
        return 0
    try:
        meaning.claude_scores(conn, ids)
    except meaning.ScorerUnavailable as e:
        print(f"Couldn't score with Claude: {e}")
        return 1
    print("Done; these scores are cached for offline layouts.")


def _sphere_compare(conn, a):
    from . import meaning, render, sphere
    if a.run is not None:
        base = sphere.load_run(conn, a.run)
        if base is None:
            print(f"No run #{a.run}.")
            return 1
        ids, seed, how = base["sample"], base["seed"], base["sample_how"]
    else:
        seed, ids, how = _sphere_sample(conn, a)
        base = None
    if not _cost_ok(conn, ids, a):
        return 1
    m_scores, m_info = meaning.meaning_relatedness(conn, ids, scorer=a.scorer)
    t_scores = sphere.tag_relatedness(conn, ids, form_weight=a.form_weight)
    runs = {}
    if base is not None:
        runs[base["stage"]] = base["id"]
        if base["stage"] == "meaning" and base["params"].get("scorer") != m_info["scorer"]:
            print(f"Note: run #{base['id']} used the '{base['params'].get('scorer')}' scorer; this comparison uses '{m_info['scorer']}'.")
    print(f"Comparing tags and meaning on {len(ids)} sayings (seed {seed})...")
    if "tags" not in runs:
        runs["tags"] = _layout(conn, a, ids, "tags", seed, how)
    if "meaning" not in runs:
        runs["meaning"] = _layout(conn, a, ids, "meaning", seed, how, related=m_scores, info=m_info)
    rep = meaning.compare(t_scores, m_scores, ids)
    cid = conn.execute("INSERT INTO comparisons (tags_run, meaning_run, report) VALUES (?,?,?)",
                       (runs["tags"], runs["meaning"], json.dumps(rep))).lastrowid
    conn.commit()
    out_dir = Path(a.out).parent if a.out else Path("export")
    md = out_dir / f"compare-{cid}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(render.compare_markdown(conn, cid), encoding="utf-8")
    page = render.write_compare_page(conn, cid, a.out or out_dir / f"compare-{cid}.html")
    text = lambda i: records.get(conn, i)["text"]
    print(f"Comparison #{cid}: agreement {rep['agreement']} over {rep['pairs']} pairs.")
    print(f"  hidden connections (close in meaning, no shared tags): {len(rep['hidden'])}")
    for h in rep["hidden"][:5]:
        print(f'    "{text(h["a"])[:50]}"  <->  "{text(h["b"])[:50]}"')
    print(f"  possible mis-tags (tagged alike, far apart in meaning): {len(rep['mistag'])}")
    for h in rep["mistag"][:5]:
        print(f'    "{text(h["a"])[:50]}"  <->  "{text(h["b"])[:50]}"')
    if m_info["scorer"] == "words":
        print(f"  ⚠ {render.WORDS_WARNING}")
    print(f"Wrote {md} and {page}")
    return _maybe_open(page, a.open)


def _maybe_open(path, do_open):
    if do_open:
        webbrowser.open(Path(path).resolve().as_uri())
    return 0


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


def _consult_sphere(conn, a, matcher):
    """consult --sphere: lay the situation's best sayings out on a sphere around it."""
    from . import meaning, render, sphere
    groups, tensions, matched = consult_mod.consult(conn, a.situation, a.n, matcher)
    found = [s for _, ss in groups for s in ss]
    if not found:
        print("No accepted sayings matched. Try different words, or --smart.")
        return 0
    top = max(s["relevance"] for s in found) or 1.0
    relevance = {s["id"]: round(s["relevance"] / top, 3) for s in found}
    # Counter-sayings of the matches come too, so what argues against the advice is on the globe.
    pulled_in = sorted({o for s in found for o in records.counters_of(conn, s["id"])
                        if o not in relevance and records.get(conn, o)["status"] == "accepted"})
    ids = sorted(relevance) + pulled_in
    if len(ids) < 4:
        print(f"Only {len(ids)} saying(s) matched; a sphere needs at least 4. Try --n higher, other words, or --smart.")
        return 1
    seed = a.seed if a.seed is not None else random.randrange(1_000_000)
    stage, related, info = a.stage, None, None
    if stage == "meaning":
        a.scorer = "claude"
        if not _cost_ok(conn, ids, a):
            return 1
        try:
            related, info = meaning.meaning_relatedness(conn, ids, scorer="claude", fallback=False)
        except meaning.ScorerUnavailable as e:
            print(f"Note: meaning scores unavailable ({e}); arranging by shared tags instead.")
            stage = "tags"
    print(f"Laying out {len(ids)} sayings around your situation "
          f"({len(relevance)} matched, {len(pulled_in)} counter-sayings pulled in; arranged by {stage}; seed {seed})...")
    run_id = sphere.run_layout(conn, ids, stage=stage, seed=seed, related=related, stage_info=info,
                               sample_how=f"consult: {a.situation[:80]}",
                               center={"text": a.situation, "relevance": relevance, "matched": matched,
                                       "pulled_in": pulled_in})
    st = sphere.load_run(conn, run_id)["stats"]
    print(f"Run #{run_id} saved. The most relevant third sit {st['top_third_mean_angle']}° from your situation on "
          f"average (all sayings: {st['mean_angle_from_situation']}°; correlation of relevance with distance "
          f"{st['relevance_vs_distance']}).")
    if st["counter_angles"]:
        print(f"  counter-sayings apart: {', '.join(f'{x}°' for x in st['counter_angles'])} (180° = opposite sides)")
    out = render.write_page(conn, run_id, a.out or Path("export") / f"sphere-{run_id}.html")
    print(f"Wrote {out}")
    return _maybe_open(out, a.open)


def cmd_consult(conn, a):
    matcher = consult_mod.load_matcher("compendium.llm_matcher:match" if a.smart else a.matcher)
    if a.sphere:
        return _consult_sphere(conn, a, matcher)
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


def sphere_defaults(key):
    from .sphere import DEFAULTS
    return DEFAULTS[key]


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
    s.add_argument("--smart", action="store_true",
                   help="let Claude pick the tags, via claude_watcher (one call; falls back to keywords)")
    s.add_argument("--matcher", help="module:function to replace the keyword matcher")
    s.add_argument("--sphere", action="store_true",
                   help="lay the best sayings out on a sphere around your situation and write a page")
    s.add_argument("-n", "--n", type=int, default=15, help="with --sphere: how many matching sayings (default 15)")
    s.add_argument("--stage", default="meaning", choices=("meaning", "tags"),
                   help="with --sphere: arrange the sayings among themselves by meaning (default, via the watcher) or tags")
    s.add_argument("--seed", type=int, help="with --sphere: random seed")
    s.add_argument("--yes", action="store_true", help="with --sphere: allow more than 10 watcher calls")
    s.add_argument("--out", help="with --sphere: HTML file to write")
    s.add_argument("--open", action="store_true", help="with --sphere: open the page in your browser")
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

    s = sub.add_parser("set-status", help="accept, reject, or un-accept sayings by id")
    s.add_argument("status", choices=STATUSES)
    s.add_argument("ids", nargs="+", type=int)
    s.add_argument("--note", help="optional review note")
    s.set_defaults(func=cmd_set_status)

    s = sub.add_parser("settings", help="show or change settings (default_status)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_settings)

    s = sub.add_parser("sphere", help="lay sayings out on a sphere and draw it (see docs/sphere-layout-spec.md)")
    s.add_argument("action", nargs="?", default="run", choices=("run", "list", "render", "compare", "score"))
    s.add_argument("run", nargs="?", type=int, help="run id (for render, or compare to reuse a run's sample)")
    s.add_argument("-n", "--n", type=int, default=15, help="how many sayings to place (default 15)")
    s.add_argument("--ids", help="comma-separated saying ids instead of a random sample")
    s.add_argument("--all", action="store_true", help="use every accepted saying (score, compare)")
    s.add_argument("--seed", type=int, help="random seed (default: new each time; printed so you can repeat it)")
    s.add_argument("--stage", default="tags", choices=("tags", "meaning"),
                   help="relatedness: shared tags (stage one) or meaning (stage two)")
    s.add_argument("--scorer", default="claude", choices=("claude", "words"),
                   help="meaning scorer: Claude (cached; falls back to words if unavailable) or offline word overlap")
    s.add_argument("--yes", action="store_true",
                   help="go ahead even if scoring needs more than 10 Claude calls (the watcher's daily limit is shared)")
    s.add_argument("--pull", type=float, default=sphere_defaults("pull"),
                   help="how strongly related sayings attract (higher = tighter clusters, less even)")
    s.add_argument("--push-counters", type=float, default=sphere_defaults("push_counters"),
                   help="how strongly counter-sayings are pushed to opposite sides")
    s.add_argument("--form-weight", type=float, default=sphere_defaults("form_weight"),
                   help="0-1: share of relatedness from having the same form")
    s.add_argument("--restarts", type=int, default=sphere_defaults("restarts"), help="random starts; best is kept")
    s.add_argument("--no-snap", action="store_true", help="don't snap to the 1,002-point lattice")
    s.add_argument("--include-candidates", action="store_true", help="sample from candidates too")
    s.add_argument("--out", help="HTML file to write (default export/sphere-<run>.html)")
    s.add_argument("--open", action="store_true", help="open the page in your browser")
    s.set_defaults(func=cmd_sphere)

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
