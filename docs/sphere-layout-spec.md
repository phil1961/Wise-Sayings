# SIDE ARTIFACT — Sayings Sphere Layout

**Status: Side artifact. NOT part of the Network Meaning game/synthesis rounds.** Extension to the standalone Sayings Compendium Engine (see "SIDE ARTIFACT \- Sayings Compendium Engine \- Build Spec for Terminal Claude").

## Latitude to extend the schema

You are explicitly free to add new fields, tables, indexes, and other structures to the Sayings database and codebase whenever this work calls for it — e.g. layout runs, sphere coordinates, lattice vertex assignments, edge tables, embedding vectors, relatedness caches, comparison reports. Do not contort the design to fit the original schema. Use migrations (or a versioned schema script) so existing data is preserved, and document every addition in the README.

## Step zero: reuse the Network Meaning sphere code

The sayings program must reach out to the Network Meaning app's code and review how its spherical mode works: how it builds the 10-frequency geodesic sphere (closest-packed "marbles," 1,002 vertices at frequency 10), how it plots Bobby's actions/events/statements onto it, and how it draws/renders the sphere. Reuse that geometry and rendering approach rather than inventing a new one. Read it; do not modify the Network Meaning code. Keep the sayings artifact standalone — copy or import what's needed, document which files it came from.

## The idea

Picture a 20-inch globe. Take 10–20 sayings from the database (random at first) and stick them at random spots on the surface. Then tell the algorithm: move them so related sayings sit near each other, in the most economical arrangement. In geodesic terms, economical \= minimum total energy, ending in an even triangulation of the sayings across the surface.

## Mechanics (same in both stages)

1. Scatter: place N sayings (10–20 to start) at random points on the unit sphere.  
2. Forces:  
   - Every pair repels (Thomson-problem style), so points spread evenly and don't pile up.  
   - Related pairs also attract with a spring whose strength \= their relatedness score.  
   - Counter-sayings (the counter\_sayings field) are pushed toward antipodal positions — opposing sayings end up on opposite sides of the globe, so tensions become geometry.  
3. Relax: iterate (gradient descent / force simulation constrained to the sphere surface) until total energy stops dropping.  
4. Triangulate: compute the spherical Delaunay triangulation (convex hull of the points) to get the geodesic network of edges between sayings.  
5. Optional snap: map final positions onto the nearest vertices of the 10-frequency geodesic lattice from the Network Meaning code, so sayings occupy marble positions.  
6. Render using the Network Meaning sphere drawing approach.

Only the relatedness measure changes between stages.

## Stage one — tags

Relatedness \= overlap of subject\_tags and situation\_tags (e.g. Jaccard similarity), optionally weighted by form. Cheap, predictable, deterministic. Limitation: it can only rediscover categories already assigned.

## Stage two — meaning

Relatedness \= semantic similarity of the saying's text plus its lens, via embeddings (cosine similarity). This is where non-obvious neighbors appear (e.g. Philip's gold-laden donkey beside a bribery proverb from another culture with no shared tags). Embedding step should be pluggable and cache vectors in the SQLite DB so layouts can be recomputed offline.

## Comparison view

Overlay or diff the stage one and stage two layouts. Flag pairs that meaning pulls together but tags keep apart — these hidden connections may be the most valuable output. Also flag the reverse (tagged alike but semantically distant), which may indicate mis-tagging.

## Outputs

- Saved layout (saying id → sphere coordinates, lattice vertex if snapped, edges) per run, with stage, seed and sample recorded.  
- Rendered sphere view.  
- Report of hidden connections and possible mis-tags.

## Later

Scale N upward toward the full accepted set; allow seeding the sample from a consult query (a situation) instead of random, so the sphere shows the neighborhood of sayings around a problem.