"""Sphere layout of sayings (docs/sphere-layout-spec.md).

Scatter N sayings on the unit sphere, then relax them under three forces:
  - every pair repels (Thomson problem), so points spread evenly;
  - related pairs attract with a spring scaled by their relatedness (0..1);
  - counter-sayings are pushed toward opposite sides of the globe.
Then triangulate (spherical Delaunay = convex hull), optionally snap to the
frequency-10 geodesic lattice, and save the run.

Only the relatedness measure differs between stages. A relatedness function is

    relatedness(conn, ids) -> {(a_id, b_id): score}   with a_id < b_id, score in 0..1

Stage one ('tags') is tag overlap. Stage two ('meaning') will plug in the same way.
"""

import json
import math
import random

from . import geodesic

DEFAULTS = {
    "pull": 3.0,          # strength of attraction between related sayings
    "push_counters": 1.0, # strength of the push that sends counter-sayings to opposite sides
    "form_weight": 0.0,   # stage one: share of relatedness that comes from having the same form
    "restarts": 6,        # independent random starts; the lowest-energy result wins
    "snap": True,
}


# ---------------------------------------------------------------- relatedness

def _tag_sets(conn, ids):
    sets = {i: set() for i in ids}
    q = ("SELECT st.saying_id, t.kind || ':' || t.name FROM saying_tags st JOIN tags t ON t.id = st.tag_id "
         f"WHERE st.saying_id IN ({','.join('?' * len(ids))})")
    for sid, tag in conn.execute(q, list(ids)):
        sets[sid].add(tag)
    return sets


def tag_relatedness(conn, ids, form_weight=0.0):
    """Stage one: Jaccard overlap of subject+situation tags, optionally blended with same-form."""
    sets = _tag_sets(conn, ids)
    forms = dict(conn.execute(f"SELECT id, form FROM sayings WHERE id IN ({','.join('?' * len(ids))})", list(ids)))
    scores = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            union = sets[a] | sets[b]
            jac = len(sets[a] & sets[b]) / len(union) if union else 0.0
            score = (1 - form_weight) * jac + form_weight * (forms[a] == forms[b])
            if score > 0:
                scores[tuple(sorted((a, b)))] = score
    return scores


STAGES = ("tags", "meaning")


def stage_relatedness(conn, ids, stage, form_weight=0.0, scorer="claude", log=None):
    """Return (scores by id pair, info dict recorded in the run's params)."""
    if stage == "tags":
        return tag_relatedness(conn, ids, form_weight=form_weight), {}
    if stage == "meaning":
        from . import meaning
        return meaning.meaning_relatedness(conn, ids, scorer=scorer, log=log)
    raise ValueError(f"unknown stage {stage!r}; available: {', '.join(STAGES)}")


def counter_pairs(conn, ids):
    wanted = set(ids)
    return {(a, b) for a, b in conn.execute("SELECT a_id, b_id FROM counter_pairs")
            if a in wanted and b in wanted}


# ---------------------------------------------------------------- forces

def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _norm(a): return math.sqrt(_dot(a, a))


def energy_and_grad(pts, related, counters, pull, push_counters):
    """Total energy and its gradient for points on the unit sphere.

    pts: list of (x,y,z). related: {(i,j): r} by index. counters: set of (i,j) by index.
    Energy per pair, with d the straight-line (chord) distance, 0..2:
        repulsion  1/d
        attraction pull * r * d^2
        counter    push_counters * N * (pi - theta)^2   (theta = angle apart; zero when antipodal)
    The counter term is scaled by N so it holds up against N-1 repulsions.
    """
    n = len(pts)
    grad = [[0.0, 0.0, 0.0] for _ in range(n)]
    total = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            diff = _sub(pts[i], pts[j])
            d = max(_norm(diff), 1e-9)
            # dE/dd for this pair
            e, de = 1 / d, -1 / d ** 2
            # Counter-sayings often share tags (both are about the same decision), but they
            # pull opposite ways, so their tag attraction is switched off.
            r = 0.0 if (i, j) in counters else related.get((i, j), 0.0)
            if r:
                e += pull * r * d * d
                de += 2 * pull * r * d
            if (i, j) in counters:
                # Penalize the angle short of 180 degrees. (A penalty on chord distance
                # goes flat near the antipode and leaves pairs stuck around 130-150 degrees.)
                k = push_counters * n
                half = math.asin(min(1.0, d / 2))           # theta / 2
                short = math.pi - 2 * half                   # pi - theta
                e += k * short ** 2
                de += -2 * k * short / max(math.cos(half), 1e-6)   # d(theta)/dd = 1 / cos(theta/2)
            total += e
            for c in range(3):
                g = de * diff[c] / d
                grad[i][c] += g
                grad[j][c] -= g
    return total, grad


def relax(pts, related, counters, pull, push_counters, max_iter=4000, tol=1e-10, fixed=()):
    """Gradient descent kept on the sphere: step along the tangent part of -grad, renormalize.

    The step shrinks when energy rises and grows a little when it falls. Stops when
    the relative energy drop stays below tol or the step becomes tiny. Points whose
    index is in `fixed` never move (a situation at the centre of a consult sphere).
    """
    pts = [geodesic.normalize(p) for p in pts]
    fixed = set(fixed)
    e, grad = energy_and_grad(pts, related, counters, pull, push_counters)
    step = 0.01
    for _ in range(max_iter):
        moved = []
        for k, (p, g) in enumerate(zip(pts, grad)):
            if k in fixed:
                moved.append(p)
                continue
            radial = _dot(g, p)
            tangent = (g[0] - radial * p[0], g[1] - radial * p[1], g[2] - radial * p[2])
            moved.append(geodesic.normalize((p[0] - step * tangent[0], p[1] - step * tangent[1],
                                             p[2] - step * tangent[2])))
        e2, grad2 = energy_and_grad(moved, related, counters, pull, push_counters)
        if e2 < e:
            done = (e - e2) / max(abs(e), 1e-12) < tol
            pts, e, grad, step = moved, e2, grad2, step * 1.2
            if done:
                break
        else:
            step *= 0.5
            if step < 1e-12:
                break
    return pts, e


def random_point(rng):
    while True:  # uniform on the sphere via rejection from the cube
        v = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1))
        n = _norm(v)
        if 0.1 < n <= 1:
            return (v[0] / n, v[1] / n, v[2] / n)


# ---------------------------------------------------------------- triangulation

def convex_hull(pts, eps=1e-12):
    """Triangles of the convex hull of points (incremental; assumes >= 4 points not all coplanar).

    For points on a sphere this is the spherical Delaunay triangulation.
    Faces are index triples oriented so their normal points outward.
    """
    n = len(pts)
    if n < 4:
        raise ValueError("need at least 4 points to triangulate")

    def normal(f):
        a, b, c = (pts[i] for i in f)
        u, v = _sub(b, a), _sub(c, a)
        return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])

    def above(f, p):
        return _dot(normal(f), _sub(pts[p], pts[f[0]]))

    # starting tetrahedron: 0, farthest from 0, farthest from that line, farthest from that plane
    i0 = 0
    i1 = max(range(n), key=lambda k: _norm(_sub(pts[k], pts[i0])))
    def line_dist(k):
        u, w = _sub(pts[i1], pts[i0]), _sub(pts[k], pts[i0])
        cr = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        return _norm(cr)
    i2 = max(range(n), key=line_dist)
    i3 = max(range(n), key=lambda k: abs(above((i0, i1, i2), k)))
    if abs(above((i0, i1, i2), i3)) < eps:
        raise ValueError("points are coplanar")
    faces = [(i0, i1, i2), (i0, i2, i3), (i0, i3, i1), (i1, i3, i2)]
    if above((i0, i1, i2), i3) > 0:  # make the first face point away from i3, the rest follow
        faces = [(i0, i2, i1), (i0, i3, i2), (i0, i1, i3), (i1, i2, i3)]

    for p in range(n):
        if p in (i0, i1, i2, i3):
            continue
        visible = [f for f in faces if above(f, p) > eps]
        if not visible:
            continue
        edges = {(f[k], f[(k + 1) % 3]) for f in visible for k in range(3)}
        horizon = [(a, b) for a, b in edges if (b, a) not in edges]
        vis = set(visible)
        faces = [f for f in faces if f not in vis] + [(a, b, p) for a, b in horizon]
    return faces


def hull_edges(faces):
    return sorted({tuple(sorted((f[k], f[(k + 1) % 3]))) for f in faces for k in range(3)})


# ---------------------------------------------------------------- quality

def angle(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(a, b)))))


def layout_stats(pts, related, counters):
    """Numbers that say whether the layout did its job.

    relatedness_vs_angle: Pearson correlation between pair relatedness and their angle
        apart (more negative = related sayings sit closer).
    related_mean_angle / unrelated_mean_angle: average angle for pairs with r > 0 vs r = 0.
    min_angle: closest pair, in degrees (evenness; ~30+ is comfortable for N = 15-20).
    counter_angles: angle between each counter pair (180 = opposite sides).
    """
    n = len(pts)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    angs = {p: angle(pts[p[0]], pts[p[1]]) for p in pairs}
    rs = [related.get(p, 0.0) for p in pairs]
    xs = [angs[p] for p in pairs]
    corr = None
    if len(pairs) > 2 and len(set(rs)) > 1:
        mr, mx = sum(rs) / len(rs), sum(xs) / len(xs)
        cov = sum((r - mr) * (x - mx) for r, x in zip(rs, xs))
        sr = math.sqrt(sum((r - mr) ** 2 for r in rs))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        corr = round(cov / (sr * sx), 3)
    rel = [angs[p] for p in pairs if related.get(p, 0) > 0]
    unrel = [angs[p] for p in pairs if related.get(p, 0) == 0]
    return {
        "relatedness_vs_angle": corr,
        "related_mean_angle": round(sum(rel) / len(rel), 1) if rel else None,
        "unrelated_mean_angle": round(sum(unrel) / len(unrel), 1) if unrel else None,
        "min_angle": round(min(xs), 1) if xs else None,
        "counter_angles": [round(angs[p], 1) for p in sorted(counters)],
    }


# ---------------------------------------------------------------- runs

def choose_sample(conn, n, seed, include_candidates=False):
    statuses = ("accepted", "candidate") if include_candidates else ("accepted",)
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM sayings WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id", statuses)]
    if n > len(ids):
        raise ValueError(f"asked for {n} sayings but only {len(ids)} are available")
    return sorted(random.Random(seed).sample(ids, n))


CENTER_ID = 0          # the situation's id in layout_edges (saying ids start at 1)
NORTH = (0.0, 1.0, 0.0)


def run_layout(conn, ids, stage="tags", seed=0, sample_how="ids", related=None, stage_info=None,
               center=None, **params):
    """Lay out the given sayings, save the run, and return its id.

    `related` ({(a_id, b_id): 0..1}) can be passed in; otherwise it is computed for
    the stage. `stage_info` (e.g. which meaning scorer ran) is saved with the params.

    `center` makes a consult sphere: {"text": situation, "relevance": {id: 0..1},
    "matched": [tags]}. The situation is held fixed at the lattice vertex nearest the
    north pole, and each saying is pulled toward it with strength
    pull * center_pull * relevance, so the most relevant sayings gather around it.
    """
    p = {**DEFAULTS, "center_pull": 2.0, **params}
    if len(ids) < 4:
        raise ValueError("need at least 4 sayings for a sphere layout")
    ids = list(ids)
    if related is None:
        related, stage_info = stage_relatedness(conn, ids, stage, form_weight=p["form_weight"],
                                                scorer=p.get("scorer", "claude"))
    p.update(stage_info or {})
    rel_by_id = related
    index = {sid: k for k, sid in enumerate(ids)}
    related = {(index[a], index[b]): r for (a, b), r in rel_by_id.items()}
    counters = {tuple(sorted((index[a], index[b]))) for a, b in counter_pairs(conn, ids)}

    n = len(ids)
    fixed, center_vertex, center_pos = (), None, None
    if center is not None:
        center_vertex = geodesic.nearest(NORTH)
        center_pos = geodesic.vertices()[center_vertex]
        for sid, rel in center["relevance"].items():
            if sid in index and rel > 0:
                related[(index[sid], n)] = p["center_pull"] * rel
        fixed = (n,)

    rng = random.Random(seed)
    best = None
    for _ in range(p["restarts"]):
        start = [random_point(rng) for _ in ids] + ([center_pos] if center else [])
        pts, e = relax(start, related, counters, p["pull"], p["push_counters"], fixed=fixed)
        if best is None or e < best[1]:
            best = (pts, e)
    pts, energy = best

    edges = hull_edges(convex_hull(pts))          # includes the situation, when there is one
    saying_pts = pts[:n]
    verts = geodesic.snap(saying_pts, taken=[center_vertex] if center else ()) if p["snap"] else [None] * n
    stats = layout_stats(saying_pts, {k: v for k, v in related.items() if k[1] < n}, counters)
    if p["snap"]:
        lat = geodesic.vertices()
        stats["max_snap_shift"] = round(max(angle(a, lat[v]) for a, v in zip(saying_pts, verts)), 1)
    center_json = None
    if center is not None:
        rels = [center["relevance"].get(sid, 0.0) for sid in ids]
        angs = [angle(pt, center_pos) for pt in saying_pts]
        stats["relevance_vs_distance"] = _pearson(rels, angs)
        top = sorted(range(n), key=lambda k: -rels[k])[:max(1, n // 3)]
        stats["top_third_mean_angle"] = round(sum(angs[k] for k in top) / len(top), 1)
        stats["mean_angle_from_situation"] = round(sum(angs) / n, 1)
        center_json = json.dumps({**center, "relevance": {str(k): v for k, v in center["relevance"].items()},
                                  "x": center_pos[0], "y": center_pos[1], "z": center_pos[2],
                                  "vertex": center_vertex}, ensure_ascii=False)

    cur = conn.execute(
        "INSERT INTO layout_runs (stage, seed, sample, sample_how, params, energy, stats, center) VALUES (?,?,?,?,?,?,?,?)",
        (stage, seed, json.dumps(ids), sample_how, json.dumps(p), energy, json.dumps(stats), center_json))
    run_id = cur.lastrowid
    conn.executemany("INSERT INTO layout_points (run_id, saying_id, x, y, z, vertex) VALUES (?,?,?,?,?,?)",
                     [(run_id, sid, *pt, v) for sid, pt, v in zip(ids, saying_pts, verts)])
    all_ids = ids + [CENTER_ID]
    edge_rows = []
    for a, b in edges:
        x, y = sorted((all_ids[a], all_ids[b]))
        r = center["relevance"].get(y, 0.0) if x == CENTER_ID else rel_by_id.get((x, y), 0.0)
        edge_rows.append((run_id, x, y, r))
    conn.executemany("INSERT INTO layout_edges (run_id, a_id, b_id, relatedness) VALUES (?,?,?,?)", edge_rows)
    conn.commit()
    return run_id


def _pearson(xs, ys):
    n = len(xs)
    if n < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sd = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return round(cov / sd, 3)


def load_run(conn, run_id):
    row = conn.execute("SELECT * FROM layout_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    run = dict(row)
    for k in ("sample", "params", "stats"):
        run[k] = json.loads(run[k])
    if run.get("center"):
        c = json.loads(run["center"])
        c["relevance"] = {int(k): v for k, v in c["relevance"].items()}
        run["center"] = c
    run["points"] = [dict(r) for r in conn.execute(
        "SELECT saying_id, x, y, z, vertex FROM layout_points WHERE run_id=?", (run_id,))]
    run["edges"] = [tuple(r) for r in conn.execute(
        "SELECT a_id, b_id, relatedness FROM layout_edges WHERE run_id=? ORDER BY a_id, b_id", (run_id,))]
    return run


# ---------------------------------------------------------------- aligning two layouts

def _quat_rotate(q, v):
    w, x, y, z = q
    # v' = q v q*, written out
    tx = 2 * (y * v[2] - z * v[1])
    ty = 2 * (z * v[0] - x * v[2])
    tz = 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty),
            v[1] + w * ty + (z * tx - x * tz),
            v[2] + w * tz + (x * ty - y * tx))


def best_rotation(src, dst):
    """Unit quaternion (w,x,y,z) rotating src points onto dst with least squared error (Horn, 1987).

    The answer is the top eigenvector of a 4x4 symmetric matrix, found by power iteration
    after shifting the matrix so its largest eigenvalue is also the largest in size.
    """
    S = [[sum(p[i] * q[j] for p, q in zip(src, dst)) for j in range(3)] for i in range(3)]
    (xx, xy, xz), (yx, yy, yz), (zx, zy, zz) = S
    N = [[xx + yy + zz, yz - zy, zx - xz, xy - yx],
         [yz - zy, xx - yy - zz, xy + yx, zx + xz],
         [zx - xz, xy + yx, -xx + yy - zz, yz + zy],
         [xy - yx, zx + xz, yz + zy, -xx - yy + zz]]
    shift = sum(abs(v) for row in N for v in row) + 1.0
    M = [[N[i][j] + (shift if i == j else 0.0) for j in range(4)] for i in range(4)]
    q = [1.0, 0.1, 0.2, 0.3]
    for _ in range(500):
        nq = [sum(M[i][j] * q[j] for j in range(4)) for i in range(4)]
        norm = math.sqrt(sum(v * v for v in nq))
        nq = [v / norm for v in nq]
        if sum((a - b) ** 2 for a, b in zip(nq, q)) < 1e-24:
            q = nq
            break
        q = nq
    return tuple(q)


def align(src, dst):
    """Rotate (and if it fits better, mirror) src onto dst. Layouts are only defined up to
    rotation and reflection, so this is how two runs on the same sample are compared."""
    best = None
    for mirror in (False, True):
        pts = [(-p[0], p[1], p[2]) for p in src] if mirror else list(src)
        q = best_rotation(pts, dst)
        moved = [_quat_rotate(q, p) for p in pts]
        err = sum(sum((a - b) ** 2 for a, b in zip(m, d)) for m, d in zip(moved, dst))
        if best is None or err < best[1]:
            best = (moved, err, mirror)
    moved, err, mirror = best
    rms_deg = math.degrees(2 * math.asin(min(1.0, math.sqrt(err / len(src)) / 2)))
    return moved, {"mirrored": mirror, "rms_angle": round(rms_deg, 1)}
