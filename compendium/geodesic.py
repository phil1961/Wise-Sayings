"""The frequency-10 icosahedral geodesic lattice (1,002 points on the unit sphere).

Ported from the Network Meaning app, D:\\Projects\\network-meaning\\public\\sphere.html,
the `MF` block ("D: the marble frame"), which builds ten shells of marbles by
subdividing each icosahedron face at frequency f and pushing the points out to
the sphere. The sayings sphere uses only the outer shell (f = 10, 10*f^2 + 2 =
1,002 points). Same icosahedron corners, same 20 faces, same order, so vertex
numbers mean the same thing in both apps. Network Meaning's other frame (Fuller's
lattice in quadrays) is not used: its outer layer is a cuboctahedron, not a sphere.
"""

import math

FREQUENCY = 10

_T = (1 + math.sqrt(5)) / 2
_CORNERS = [(-1, _T, 0), (1, _T, 0), (-1, -_T, 0), (1, -_T, 0), (0, -1, _T), (0, 1, _T),
            (0, -1, -_T), (0, 1, -_T), (_T, 0, -1), (_T, 0, 1), (-_T, 0, -1), (-_T, 0, 1)]
_FACES = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4),
          (11, 10, 2), (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8),
          (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]


def normalize(v):
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return (v[0] / n, v[1] / n, v[2] / n)


def lattice(f=FREQUENCY):
    """Unit-sphere points of the frequency-f geodesic sphere, deduplicated, in build order."""
    corners = [normalize(c) for c in _CORNERS]
    points, seen = [], {}
    for a, b, c in _FACES:
        A, B, C = corners[a], corners[b], corners[c]
        for i in range(f + 1):
            for j in range(f + 1 - i):
                p = normalize(tuple(A[k] + (B[k] - A[k]) * i / f + (C[k] - A[k]) * j / f for k in range(3)))
                key = tuple(round(x * 1e4) for x in p)  # same dedupe rule as the JS
                if key not in seen:
                    seen[key] = len(points)
                    points.append(p)
    return points


_CACHE = {}


def vertices():
    if FREQUENCY not in _CACHE:
        _CACHE[FREQUENCY] = lattice(FREQUENCY)
    return _CACHE[FREQUENCY]


def snap(points):
    """Give each point its nearest free lattice vertex (greedy, like the JS `snap`).

    Points are handled in order of how close their nearest vertex is, so the points
    that fit the lattice best claim their vertex first. Returns a list of vertex indexes.
    """
    verts = vertices()
    ranked = []
    for idx, p in enumerate(points):
        order = sorted(range(len(verts)), key=lambda v: -_dot(p, verts[v]))
        ranked.append((-_dot(p, verts[order[0]]), idx, order))
    taken, result = set(), [None] * len(points)
    for _, idx, order in sorted(ranked):
        v = next(v for v in order if v not in taken)
        taken.add(v)
        result[idx] = v
    return result


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
