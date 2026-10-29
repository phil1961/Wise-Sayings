"""Write sphere layouts as self-contained HTML pages (open offline, no server).

A page holds one or more views of the same sayings; a comparison is two views
(stage one and stage two) turning together, with hidden connections and possible
mis-tags highlighted.
"""

import json
from pathlib import Path

from . import geodesic, records, report, sphere

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "sphere_template.html"
THREE_JS = HERE / "vendor" / "three-r128.min.js"


WORDS_WARNING = ("'meaning' here is the offline word-overlap scorer, which sees shared words, not shared ideas "
                 "(e.g. 'lost' in two unrelated sayings). Treat its hidden connections as noise; "
                 "use the Claude scorer for real ones.")


def _script_safe(text):
    # Stop "</script>" inside inlined text from closing the script tag early.
    return text.replace("</script", "<\\/script")


def _sayings(conn, ids):
    out = []
    for sid in ids:
        s = records.get(conn, sid)
        out.append({"id": sid, "text": s["text"], "who": report.attribution_line(s), "form": s["form"],
                    "lens": s["lens"], "impact_score": s["impact_score"],
                    "tags": s["subject_tags"] + s["situation_tags"]})
    return out


def _edges(conn, run):
    """Edges with their relatedness. Runs made before edge scores were stored get tag scores recomputed."""
    if all(r is not None for _, _, r in run["edges"]):
        return [[a, b, round(r, 3)] for a, b, r in run["edges"]]
    rel = sphere.tag_relatedness(conn, run["sample"], form_weight=run["params"].get("form_weight", 0.0))
    return [[a, b, round(rel.get((a, b), 0.0), 3)] for a, b, _ in run["edges"]]


def stage_label(run):
    if run["stage"] == "tags":
        return "by tags"
    scorer = run["params"].get("scorer", "?")
    return "by meaning (" + (f"Claude, {run['params'].get('model')}" if scorer == "claude" else "word overlap, offline") + ")"


def _write(data, out_path):
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__THREE__*/", _script_safe(THREE_JS.read_text(encoding="utf-8")), 1)
    html = html.replace("/*__DATA__*/", _script_safe(json.dumps(data, ensure_ascii=False)), 1)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


def write_page(conn, run_id, out_path):
    run = sphere.load_run(conn, run_id)
    if run is None:
        raise ValueError(f"no layout run #{run_id}")
    ids = run["sample"]
    center = run.get("center")
    data = {
        "title": "Around your situation" if center else "Sayings sphere",
        "subtitle": f"Run {run['id']} · {stage_label(run)} · {len(ids)} sayings · seed {run['seed']}"
                    + (" · on the frequency-10 lattice" if run["params"].get("snap") else ""),
        "center": None if not center else {
            "text": center["text"], "matched": center.get("matched", []),
            "pos": [center["x"], center["y"], center["z"]],
            "relevance": {str(k): v for k, v in center["relevance"].items()},
            "pulled_in": center.get("pulled_in", []),
        },
        "sayings": _sayings(conn, ids),
        "views": [{"title": stage_label(run), "points": run["points"], "edges": _edges(conn, run)}],
        "counters": sorted(sphere.counter_pairs(conn, ids)),
        "lattice": [[round(c, 5) for c in v] for v in geodesic.vertices()],
    }
    return _write(data, out_path)


def write_compare_page(conn, comparison_id, out_path):
    row = conn.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
    if row is None:
        raise ValueError(f"no comparison #{comparison_id}")
    tags_run, meaning_run = sphere.load_run(conn, row["tags_run"]), sphere.load_run(conn, row["meaning_run"])
    rep = json.loads(row["report"])
    ids = meaning_run["sample"]
    # Layouts are only defined up to rotation and mirroring: turn the tags layout to best match the meaning one.
    order = {p["saying_id"]: p for p in tags_run["points"]}
    mpts = {p["saying_id"]: p for p in meaning_run["points"]}
    src = [(order[i]["x"], order[i]["y"], order[i]["z"]) for i in ids]
    dst = [(mpts[i]["x"], mpts[i]["y"], mpts[i]["z"]) for i in ids]
    moved, fit = sphere.align(src, dst)
    as_points = lambda pts: [{"saying_id": i, "x": p[0], "y": p[1], "z": p[2], "vertex": None} for i, p in zip(ids, pts)]
    data = {
        "title": "Tags vs meaning",
        "subtitle": f"Comparison {comparison_id} · runs {tags_run['id']} and {meaning_run['id']} · {len(ids)} sayings · "
                    f"agreement {rep['agreement']} · {len(rep['hidden'])} hidden connections · {len(rep['mistag'])} possible mis-tags"
                    + (" · ⚠ " + WORDS_WARNING if meaning_run["params"].get("scorer") == "words" else ""),
        "sayings": _sayings(conn, ids),
        "views": [
            {"title": stage_label(tags_run) + (" (mirrored to match)" if fit["mirrored"] else ""),
             "points": as_points(moved), "edges": _edges(conn, tags_run)},
            {"title": stage_label(meaning_run), "points": as_points(dst), "edges": _edges(conn, meaning_run)},
        ],
        "counters": sorted(sphere.counter_pairs(conn, ids)),
        "highlights": {"hidden": rep["hidden"], "mistag": rep["mistag"]},
        "lattice": [[round(c, 5) for c in v] for v in geodesic.vertices()],
    }
    return _write(data, out_path)


def compare_markdown(conn, comparison_id):
    row = conn.execute("SELECT * FROM comparisons WHERE id=?", (comparison_id,)).fetchone()
    rep = json.loads(row["report"])
    meaning_run = sphere.load_run(conn, row["meaning_run"])
    text = lambda i: records.get(conn, i)["text"]
    lines = [f"# Tags vs meaning: comparison {comparison_id}", "",
             f"Runs {row['tags_run']} (tags) and {row['meaning_run']} ({stage_label(meaning_run)}), "
             f"{len(meaning_run['sample'])} sayings, {rep['pairs']} pairs.", "",
             f"**Agreement** (rank correlation of the two scores over all pairs): {rep['agreement']}. "
             "1 means tags and meaning rank pairs the same way; 0 means unrelated.", ""]
    if meaning_run["params"].get("scorer") == "words":
        lines += [f"> **Warning:** {WORDS_WARNING}", ""]
    lines += [
             "## Hidden connections", "",
             "Pairs in the top 10% by meaning that share no tags. Candidates for a shared tag, "
             "or simply the non-obvious neighbours the sphere is for.", ""]
    for h in rep["hidden"] or []:
        lines.append(f"- #{h['a']} \"{text(h['a'])}\"  \n  #{h['b']} \"{text(h['b'])}\"  \n  meaning {h['meaning']:.0%}, tags {h['tags']:.0%}")
    if not rep["hidden"]:
        lines.append("- none in this sample")
    lines += ["", "## Possible mis-tags", "",
              "Pairs sharing at least 40% of their tags but in the bottom 25% by meaning. "
              "A tag on one of them may be too broad or wrong.", ""]
    for h in rep["mistag"] or []:
        lines.append(f"- #{h['a']} \"{text(h['a'])}\"  \n  #{h['b']} \"{text(h['b'])}\"  \n  tags {h['tags']:.0%}, meaning {h['meaning']:.0%}")
    if not rep["mistag"]:
        lines.append("- none in this sample")
    return "\n".join(lines) + "\n"
