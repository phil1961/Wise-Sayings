"""Write a saved sphere layout as one self-contained HTML page (opens offline, no server)."""

import json
from pathlib import Path

from . import geodesic, records, report, sphere

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "sphere_template.html"
THREE_JS = HERE / "vendor" / "three-r128.min.js"


def _script_safe(text):
    # Stop "</script>" inside inlined text from closing the script tag early.
    return text.replace("</script", "<\\/script")


def page_data(conn, run):
    ids = run["sample"]
    if run["stage"] == "tags":
        related = sphere.tag_relatedness(conn, ids, form_weight=run["params"].get("form_weight", 0.0))
    else:
        related = sphere.STAGES[run["stage"]](conn, ids)
    sayings = []
    for sid in ids:
        s = records.get(conn, sid)
        sayings.append({"id": sid, "text": s["text"], "who": report.attribution_line(s), "form": s["form"],
                        "lens": s["lens"], "impact_score": s["impact_score"],
                        "tags": s["subject_tags"] + s["situation_tags"]})
    return {
        "run": {k: run[k] for k in ("id", "stage", "seed", "sample", "sample_how", "params", "stats", "created_at")},
        "sayings": sayings,
        "points": run["points"],
        "edges": [[a, b, round(related.get((a, b), 0.0), 3)] for a, b in run["edges"]],
        "counters": sorted(sphere.counter_pairs(conn, ids)),
        "lattice": [[round(c, 5) for c in v] for v in geodesic.vertices()],
    }


def write_page(conn, run_id, out_path):
    run = sphere.load_run(conn, run_id)
    if run is None:
        raise ValueError(f"no layout run #{run_id}")
    data = json.dumps(page_data(conn, run), ensure_ascii=False)
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__THREE__*/", _script_safe(THREE_JS.read_text(encoding="utf-8")), 1)
    html = html.replace("/*__DATA__*/", _script_safe(data), 1)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
