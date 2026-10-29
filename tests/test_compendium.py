"""Run with:  python -m unittest discover tests"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from compendium import consult, db, records, report, review

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "starter.json"


def base(**over):
    rec = {
        "text": "Look before you leap.", "form": "proverb", "attribution": None,
        "attribution_confidence": "anonymous", "documented_in": "Heywood, Proverbs (1546)",
        "lens": "Check the landing.", "impact_score": 6, "impact_rationale": "Familiar.",
        "subject_tags": ["prudence"], "situation_tags": ["hasty_decision"],
    }
    rec.update(over)
    return rec


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn, _ = db.connect(self.dir / "t.db", db.DEFAULT_VOCAB)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def accept_all(self):
        self.conn.execute("UPDATE sayings SET status='accepted'")
        self.conn.commit()


class TestValidation(Base):
    def test_seed_imports_cleanly(self):
        res = records.import_records(self.conn, records.load_file(SEED))
        self.assertEqual(res["errors"], [])
        self.assertGreaterEqual(len(res["added"]), 20)

    def test_documented_in_required(self):
        res = records.import_records(self.conn, [base(documented_in="  ")])
        self.assertTrue(any("documented_in" in e for e in res["errors"]))

    def test_anonymous_must_have_no_attribution(self):
        res = records.import_records(self.conn, [base(attribution="Someone")])
        self.assertTrue(any("anonymous" in e for e in res["errors"]))

    def test_named_confidence_needs_attribution(self):
        res = records.import_records(self.conn, [base(attribution_confidence="verified")])
        self.assertTrue(any("attribution required" in e for e in res["errors"]))

    def test_unknown_tag_rejected(self):
        res = records.import_records(self.conn, [base(situation_tags=["not_a_tag"])])
        self.assertTrue(any("not in the vocabulary" in e for e in res["errors"]))

    def test_batch_is_all_or_nothing(self):
        res = records.import_records(self.conn, [base(), base(text="Other.", impact_score=11)])
        self.assertEqual(res["added"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sayings").fetchone()[0], 0)

    def test_exact_duplicate_rejected(self):
        records.import_records(self.conn, [base()])
        res = records.import_records(self.conn, [base(text="look before you leap.")])
        self.assertTrue(any("already exists" in e for e in res["errors"]))

    def test_csv_import_with_semicolon_lists(self):
        csv_path = self.dir / "b.csv"
        csv_path.write_text(
            "text,form,attribution_confidence,documented_in,lens,impact_score,impact_rationale,"
            "subject_tags,situation_tags\n"
            "Look before you leap.,proverb,anonymous,Heywood,Check.,6,Familiar.,prudence;risk,"
            "hasty_decision;commitment\n", encoding="utf-8")
        res = records.import_records(self.conn, records.load_file(csv_path))
        self.assertEqual(res["errors"], [])
        s = records.get(self.conn, res["added"][0])
        self.assertEqual(s["subject_tags"], ["prudence", "risk"])


class TestCountersAndDedupe(Base):
    def test_counter_by_text_is_symmetric(self):
        records.import_records(self.conn, [
            base(),
            base(text="He who hesitates is lost.", counter_sayings=["Look before you leap."]),
        ])
        self.assertEqual(records.counters_of(self.conn, 1), [2])
        self.assertEqual(records.counters_of(self.conn, 2), [1])

    def test_near_duplicate_flagged(self):
        records.import_records(self.conn, [base()])
        res = records.import_records(self.conn, [base(text="Look well before you leap.")])
        self.assertEqual(len(res["duplicates"]), 1)


class TestReview(Base):
    def test_export_then_apply(self):
        records.import_records(self.conn, [base(), base(text="He who hesitates is lost.")])
        path = self.dir / "review.md"
        self.assertEqual(review.export_candidates(self.conn, path), 2)
        text = path.read_text(encoding="utf-8")
        text = text.replace("Decision: \n", "Decision: accept\n", 1)
        text = text.replace("Decision: \n", "Decision: reject | too familiar\n", 1)
        path.write_text(text, encoding="utf-8")
        applied, problems = review.apply_decisions(self.conn, path)
        self.assertEqual(applied, {"accepted": 1, "rejected": 1})
        self.assertEqual(problems, [])
        self.assertEqual(records.get(self.conn, 2)["review_note"], "too familiar")

    def test_blank_decisions_are_skipped(self):
        records.import_records(self.conn, [base()])
        path = self.dir / "review.md"
        review.export_candidates(self.conn, path)
        applied, _ = review.apply_decisions(self.conn, path)
        self.assertEqual(applied, {"accepted": 0, "rejected": 0})


class TestConsult(Base):
    def setUp(self):
        super().setUp()
        records.import_records(self.conn, records.load_file(SEED))

    def test_only_accepted_sayings_returned(self):
        groups, _, _ = consult.consult(self.conn, "a bigger competitor threatens us")
        self.assertEqual(groups, [])

    def test_relevant_results_and_tensions(self):
        self.accept_all()
        groups, tensions, matched = consult.consult(self.conn, "should I hesitate or commit now?")
        self.assertIn("commitment", matched)
        texts = [s["text"] for _, ss in groups for s in ss]
        self.assertIn("He who hesitates is lost.", texts)
        pairs = {frozenset((a["text"], b["text"])) for a, b, _ in tensions}
        self.assertIn(frozenset(("Look before you leap.", "He who hesitates is lost.")), pairs)

    def test_negated_alias_does_not_match_plain_word(self):
        self.accept_all()
        _, _, matched = consult.consult(self.conn, "a competitor is undercutting us")
        self.assertNotIn("weak_competition", matched)

    def test_limit_respected(self):
        self.accept_all()
        groups, _, _ = consult.consult(self.conn, "war power risk plan negotiation threat", limit=5)
        self.assertLessEqual(sum(len(ss) for _, ss in groups), 5)

    def test_pluggable_matcher(self):
        self.accept_all()
        tag_id = self.conn.execute("SELECT id FROM tags WHERE name='keeping_secrets'").fetchone()[0]
        groups, _, _ = consult.consult(self.conn, "anything", matcher=lambda s, t: {tag_id: 3.0})
        self.assertIn("Three may keep a secret", groups[0][1][0]["text"])


class TestExportAndVocab(Base):
    def test_export_json_roundtrip(self):
        records.import_records(self.conn, records.load_file(SEED))
        self.accept_all()
        out = self.dir / "c.json"
        n = report.export_json(self.conn, out)
        self.assertEqual(len(json.loads(out.read_text(encoding="utf-8"))), n)

    def test_removed_tag_kept_while_in_use(self):
        records.import_records(self.conn, [base()])
        vocab = self.dir / "tags.txt"
        vocab.write_text("[subject]\nprudence\n[situation]\nnegotiation\n", encoding="utf-8")
        orphans = db.sync_vocab(self.conn, vocab)
        self.assertEqual(orphans, ["situation:hasty_decision"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE name='commitment'").fetchone())


class TestSmartMatcher(Base):
    """The LLM matcher must map Claude's reply to tag weights and fall back safely."""

    def setUp(self):
        super().setUp()
        records.import_records(self.conn, records.load_file(SEED))
        self.accept_all()
        self.tags = [dict(r, aliases=json.loads(r["aliases"])) for r in
                     self.conn.execute("SELECT id, name, kind, aliases FROM tags")]

    def tag_id(self, name):
        return self.conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]

    def test_reply_to_weights(self):
        from compendium import llm_matcher
        reply = {"tags": [{"id": "situation:betrayal", "relevance": 3},
                          {"id": "subject:human_nature", "relevance": 3},
                          {"id": "situation:not_real", "relevance": 3}]}
        w = llm_matcher.weights_from_reply(reply, self.tags)
        self.assertEqual(w, {self.tag_id("betrayal"): 3.0, self.tag_id("human_nature"): 2.0})

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
    def test_fake_client_drives_consult(self):
        from types import SimpleNamespace as NS
        from compendium import llm_matcher

        class FakeMessages:
            def create(self, **kw):
                self.kw = kw
                text = json.dumps({"tags": [{"id": "situation:betrayal", "relevance": 3}]})
                return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])

        fake = NS(beta=NS(messages=FakeMessages()))
        matcher = lambda s, t: llm_matcher.match(s, t, client=fake)
        groups, _, matched = consult.consult(self.conn, "my cofounder keeps meeting our rival", matcher=matcher)
        self.assertEqual(matched, ["betrayal"])
        self.assertEqual(groups[0][0], "betrayal")
        self.assertEqual(fake.beta.messages.kw["fallbacks"], "default")

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
    def test_real_sdk_request_falls_back_when_unreachable(self):
        import anthropic
        from compendium import llm_matcher
        client = anthropic.Anthropic(api_key="test", base_url="http://127.0.0.1:9", max_retries=0, timeout=3)
        w = llm_matcher.match("should I hesitate or commit now?", self.tags, client=client)
        self.assertIn(self.tag_id("commitment"), w)  # keyword fallback result


class TestStatusAndMigrations(Base):
    def test_default_status_setting_applies_to_imports(self):
        db.set_setting(self.conn, "default_status", "accepted")
        res = records.import_records(self.conn, [base()])
        self.assertEqual(records.get(self.conn, res["added"][0])["status"], "accepted")

    def test_set_status_reports_missing(self):
        records.import_records(self.conn, [base()])
        self.assertEqual(records.set_status(self.conn, [1, 99], "rejected"), [99])
        self.assertEqual(records.get(self.conn, 1)["status"], "rejected")

    def test_old_database_is_migrated_without_losing_data(self):
        import sqlite3
        old = self.dir / "old.db"
        raw = sqlite3.connect(old)
        raw.executescript(db.SCHEMA)  # a pre-migration database: schema v1, user_version 0
        raw.execute("INSERT INTO sayings (text, form, attribution, attribution_confidence, documented_in, lens, "
                    "impact_score, impact_rationale) VALUES ('X.', 'proverb', NULL, 'anonymous', 'src', 'l', 5, 'r')")
        raw.commit(); raw.close()
        conn, _ = db.connect(old, db.DEFAULT_VOCAB)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], max(db.MIGRATIONS))
        self.assertEqual(conn.execute("SELECT text FROM sayings").fetchone()[0], "X.")
        conn.execute("SELECT * FROM layout_runs")  # new table exists
        conn.close()


class TestGeodesic(unittest.TestCase):
    def test_lattice_counts(self):
        from compendium import geodesic
        self.assertEqual(len(geodesic.lattice()), 1002)
        self.assertEqual([len(geodesic.lattice(f)) for f in (1, 2, 3)], [12, 42, 92])  # 10f^2 + 2

    def test_snap_gives_distinct_nearby_vertices(self):
        from compendium import geodesic, sphere
        import random as _r
        rng = _r.Random(1)
        pts = [sphere.random_point(rng) for _ in range(40)]
        verts = geodesic.snap(pts)
        self.assertEqual(len(set(verts)), 40)
        lat = geodesic.vertices()
        self.assertLess(max(sphere.angle(p, lat[v]) for p, v in zip(pts, verts)), 8)


class TestHull(unittest.TestCase):
    def test_octahedron(self):
        from compendium import sphere
        pts = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        faces = sphere.convex_hull(pts)
        self.assertEqual((len(faces), len(sphere.hull_edges(faces))), (8, 12))

    def test_euler_counts_for_random_points(self):
        from compendium import sphere
        import random as _r
        rng = _r.Random(5)
        for n in (4, 10, 30, 60):
            pts = [sphere.random_point(rng) for _ in range(n)]
            faces = sphere.convex_hull(pts)
            self.assertEqual(len(faces), 2 * n - 4)
            self.assertEqual(len(sphere.hull_edges(faces)), 3 * n - 6)


class TestSphereLayout(Base):
    def setUp(self):
        super().setUp()
        records.import_records(self.conn, records.load_file(SEED))
        self.accept_all()

    def test_tag_relatedness_is_jaccard(self):
        from compendium import sphere
        # 11 "Look before you leap" {prudence, risk, hasty_decision, commitment}
        # 12 "He who hesitates"     {timing, risk, paralysis, commitment}: 2 shared of 6
        r = sphere.tag_relatedness(self.conn, [11, 12])
        self.assertAlmostEqual(r[(11, 12)], 2 / 6)

    def test_run_is_saved_reproducible_and_does_its_job(self):
        from compendium import sphere
        ids = sphere.choose_sample(self.conn, 14, seed=3) 
        ids = sorted(set(ids) | {11, 12})  # include a counter pair
        a = sphere.load_run(self.conn, sphere.run_layout(self.conn, ids, seed=4, restarts=2))
        b = sphere.load_run(self.conn, sphere.run_layout(self.conn, ids, seed=4, restarts=2))
        self.assertEqual([(p["x"], p["vertex"]) for p in a["points"]], [(p["x"], p["vertex"]) for p in b["points"]])
        self.assertEqual(len(a["edges"]), 3 * len(ids) - 6)
        st = a["stats"]
        self.assertLess(st["related_mean_angle"], st["unrelated_mean_angle"])
        self.assertTrue(all(x > 150 for x in st["counter_angles"]), st["counter_angles"])
        self.assertGreater(st["min_angle"], 10)

    def test_sample_size_is_checked(self):
        from compendium import sphere
        with self.assertRaises(ValueError):
            sphere.choose_sample(self.conn, 999, seed=1)
        with self.assertRaises(ValueError):
            sphere.run_layout(self.conn, [1, 2, 3])

    def test_render_writes_self_contained_page(self):
        from compendium import render, sphere
        run_id = sphere.run_layout(self.conn, sphere.choose_sample(self.conn, 8, seed=1), seed=1, restarts=1)
        html = render.write_page(self.conn, run_id, self.dir / "s.html").read_text(encoding="utf-8")
        self.assertIn("Three.js Authors", html)
        self.assertNotIn("/*__DATA__*/", html)
        data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(len(data["sayings"]), 8)
        self.assertEqual(len(data["lattice"]), 1002)


if __name__ == "__main__":
    unittest.main()
