"""Run with:  python -m unittest discover tests"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import os
from unittest import mock

from compendium import consult, db, records, report, review
from compendium import watcher as _watcher

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed" / "starter.json"

# Tests must never call the real claude_watcher: its one slot and 200 calls a day are
# shared with other apps. Cut the client off from the project's .env and environment.
_watcher.ENV_FILE = ROOT / "tests" / "no-such.env"
for _k in ("WATCHER_TOKEN", "WATCHER_URL"):
    os.environ.pop(_k, None)


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


class FakeStream:
    """Stands in for client.beta.messages.stream(...): answers with scores for the asked pairs."""
    def __init__(self, owner, kw):
        self.owner, self.kw = owner, kw
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get_final_message(self):
        from types import SimpleNamespace as NS
        import re
        asked = re.findall(r"^(\d+)-(\d+)$", self.kw["messages"][0]["content"], re.M)
        pairs = [(int(a), int(b)) for a, b in asked]
        self.owner.calls.append(pairs)
        if self.owner.skip_first and len(self.owner.calls) == 1:
            pairs = pairs[1:]  # the model "forgets" one pair; the scorer should ask again
        items = [{"a": a, "b": b, "score": (a + b) % 11} for a, b in pairs]
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps({"scores": items}))])


class FakeClient:
    def __init__(self, skip_first=False):
        from types import SimpleNamespace as NS
        self.calls, self.skip_first = [], skip_first
        self.beta = NS(messages=NS(stream=lambda **kw: FakeStream(self, kw)))


class TestMeaning(Base):
    def setUp(self):
        super().setUp()
        records.import_records(self.conn, records.load_file(SEED))
        self.accept_all()

    def test_word_scores_are_in_range_and_stable(self):
        from compendium import meaning
        ids = [1, 2, 3, 4, 5, 6]
        a, b = meaning.word_scores(self.conn, ids), meaning.word_scores(self.conn, ids)
        self.assertEqual(a, b)
        self.assertTrue(all(0 < v <= 1 for v in a.values()))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
    def test_claude_scores_cache_retry_and_invalidation(self):
        from compendium import meaning
        ids = [1, 2, 3, 4, 5]
        client = FakeClient(skip_first=True)
        s = meaning.claude_scores(self.conn, ids, client=client, log=lambda m: None)
        self.assertEqual(len(s), 10)
        self.assertEqual(len(client.calls), 2)              # first answer was missing a pair -> one retry
        self.assertEqual(s[(1, 2)], ((1 + 2) % 11) / 10)
        self.assertEqual(client.calls[0][0], (1, 2))
        again = FakeClient()
        self.assertEqual(meaning.claude_scores(self.conn, ids, client=again, log=lambda m: None), s)
        self.assertEqual(again.calls, [])                    # everything came from the cache
        self.conn.execute("UPDATE sayings SET lens = lens || ' (edited)' WHERE id = 3")
        third = FakeClient()
        meaning.claude_scores(self.conn, ids, client=third, log=lambda m: None)
        self.assertEqual(sorted(third.calls[0]), [(1, 3), (2, 3), (3, 4), (3, 5)])  # only saying 3's pairs

    def test_falls_back_to_words_when_claude_unavailable(self):
        from compendium import meaning
        class Broken:
            def __getattr__(self, name):
                raise meaning.ScorerUnavailable("no key")
        notes = []
        scores, info = meaning.meaning_relatedness(self.conn, [1, 2, 3, 4], client=Broken(), log=notes.append)
        self.assertEqual(info, {"scorer": "words"})
        self.assertTrue(any("offline" in n for n in notes), notes)

    def test_compare_finds_hidden_and_mistag_pairs(self):
        from compendium import meaning
        ids = list(range(1, 11))
        pairs = [(a, b) for a in ids for b in ids if a < b]
        tags = {p: 0.5 for p in pairs[:10]}                   # first 10 pairs tagged alike
        mean = {p: 0.05 for p in pairs}
        mean[(9, 10)] = 0.95                                   # close in meaning, no shared tags -> hidden
        mean[pairs[0]] = 0.0                                   # tagged alike, far in meaning -> mis-tag
        rep = meaning.compare(tags, mean, ids)
        self.assertIn({"a": 9, "b": 10, "meaning": 0.95, "tags": 0.0}, rep["hidden"])
        self.assertEqual(rep["mistag"][0]["a"], pairs[0][0])
        same = meaning.compare(tags, tags, ids)
        self.assertEqual(same["agreement"], 1.0)

    def test_meaning_run_and_comparison_page(self):
        from compendium import meaning, render, sphere
        ids = sphere.choose_sample(self.conn, 10, seed=2)
        m, info = meaning.meaning_relatedness(self.conn, ids, scorer="words")
        t_run = sphere.run_layout(self.conn, ids, stage="tags", seed=2, restarts=1)
        m_run = sphere.run_layout(self.conn, ids, stage="meaning", seed=2, related=m, stage_info=info, restarts=1)
        run = sphere.load_run(self.conn, m_run)
        self.assertEqual(run["params"]["scorer"], "words")
        self.assertTrue(all(r is not None for _, _, r in run["edges"]))
        rep = meaning.compare(sphere.tag_relatedness(self.conn, ids), m, ids)
        cid = self.conn.execute("INSERT INTO comparisons (tags_run, meaning_run, report) VALUES (?,?,?)",
                                (t_run, m_run, json.dumps(rep))).lastrowid
        html = render.write_compare_page(self.conn, cid, self.dir / "c.html").read_text(encoding="utf-8")
        data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(len(data["views"]), 2)
        self.assertIn("word-overlap", data["subtitle"])     # the offline warning is on the page
        self.assertIn("Warning", render.compare_markdown(self.conn, cid))


class TestAlign(unittest.TestCase):
    def test_recovers_rotation_and_mirror(self):
        import math, random as _r
        from compendium import sphere
        rng = _r.Random(3)
        pts = [sphere.random_point(rng) for _ in range(15)]
        q = (math.cos(0.6), math.sin(0.6), 0.0, 0.0)
        dst = [sphere._quat_rotate(q, (-p[0], p[1], p[2])) for p in pts]
        moved, fit = sphere.align(pts, dst)
        self.assertTrue(fit["mirrored"])
        self.assertLess(fit["rms_angle"], 0.01)


class TestWatcher(Base):
    """The watcher client and the two callers that use it, with the network mocked out."""

    def _http_error(self, code, body):
        import io, urllib.error
        return urllib.error.HTTPError("https://w/ask", code, "x", {}, io.BytesIO(json.dumps(body).encode()))

    def _ok(self, raw):
        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return json.dumps({"ok": True, "raw": raw, "usage": {}}).encode()
        return Resp()

    def test_not_configured(self):
        self.assertFalse(_watcher.configured())
        with self.assertRaises(_watcher.WatcherUnavailable):
            _watcher.ask("hi", "claude-opus-5-5")

    def test_refuses_plain_http(self):
        with mock.patch.dict(os.environ, {"WATCHER_TOKEN": "t", "WATCHER_URL": "http://w/ask"}):
            with self.assertRaises(_watcher.WatcherUnavailable):
                _watcher.ask("hi", "claude-opus-5-5")

    def test_token_from_env_file_and_busy_retry(self):
        env = self.dir / ".env"
        env.write_text("OTHER=1\nWATCHER_TOKEN='secret'\n", encoding="utf-8")
        sent = []
        def fake_urlopen(req, timeout):
            sent.append(req)
            if len(sent) == 1:
                raise self._http_error(503, {"ok": False, "code": "busy", "message": "busy"})
            return self._ok({"reply": "OK"})
        with mock.patch.object(_watcher, "ENV_FILE", env), mock.patch.object(_watcher, "BUSY_WAITS", (0,)), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            raw, _ = _watcher.ask("hi", "claude-opus-5-5", schema={"type": "object"})
        self.assertEqual(raw, {"reply": "OK"})
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].get_header("X-watcher-token"), "secret")
        body = json.loads(sent[0].data)
        self.assertEqual((body["caller"], body["model"]), ("wise-sayings", "claude-opus-5-5"))

    def test_daily_limit_is_reported(self):
        def fake_urlopen(req, timeout):
            raise self._http_error(429, {"ok": False, "code": "daily_limit", "message": "limit"})
        with mock.patch.dict(os.environ, {"WATCHER_TOKEN": "t"}), mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaisesRegex(_watcher.WatcherUnavailable, "daily call limit"):
                _watcher.ask("hi", "claude-opus-5-5")

    def test_meaning_scores_go_through_watcher(self):
        from compendium import meaning
        records.import_records(self.conn, records.load_file(SEED))
        prompts = []
        def fake_ask(prompt, model, schema=None, log=None, **kw):
            prompts.append(prompt)
            import re
            pairs = re.findall(r"^(\d+)-(\d+)$", prompt, re.M)
            return {"scores": [{"a": int(a), "b": int(b), "score": 5} for a, b in pairs]}, {}
        notes = []
        with mock.patch.object(_watcher, "ask", fake_ask):
            s = meaning.claude_scores(self.conn, [1, 2, 3, 4], log=notes.append)
        self.assertEqual(len(s), 6)
        self.assertTrue(prompts[0].startswith(meaning.SYSTEM))   # fixed part first, for the cache
        self.assertIn("claude_watcher", notes[0])

    def test_meaning_falls_back_when_watcher_unavailable(self):
        from compendium import meaning
        records.import_records(self.conn, records.load_file(SEED))
        _, info = meaning.meaning_relatedness(self.conn, [1, 2, 3, 4], log=lambda m: None)
        self.assertEqual(info, {"scorer": "words"})   # no token in tests -> offline scorer

    def test_smart_matcher_goes_through_watcher(self):
        from compendium import llm_matcher
        records.import_records(self.conn, records.load_file(SEED))
        tags = [dict(r, aliases=json.loads(r["aliases"])) for r in
                self.conn.execute("SELECT id, name, kind, aliases FROM tags")]
        with mock.patch.object(_watcher, "ask", lambda *a, **k: ({"tags": [{"id": "situation:betrayal", "relevance": 3}]}, {})):
            w = llm_matcher.match("my cofounder meets our rival", tags)
        betrayal = next(t["id"] for t in tags if t["name"] == "betrayal")
        self.assertEqual(w, {betrayal: 3.0})


class TestConsultSphere(Base):
    def setUp(self):
        super().setUp()
        records.import_records(self.conn, records.load_file(SEED))
        self.accept_all()

    def test_consult_reports_relevance(self):
        groups, _, _ = consult.consult(self.conn, "should I hesitate or commit now?")
        s = groups[0][1][0]
        self.assertGreater(s["relevance"], 0)
        self.assertAlmostEqual(s["score"], round(s["relevance"] * s["impact_score"], 1), places=0)

    def test_centered_layout_puts_relevant_sayings_near_the_situation(self):
        from compendium import geodesic, render, sphere
        ids = list(range(1, 17))
        relevance = {i: (1.0 if i <= 5 else 0.1) for i in ids}
        run_id = sphere.run_layout(self.conn, ids, seed=1, restarts=2,
                                   center={"text": "a test situation", "relevance": relevance, "matched": ["risk"],
                                           "pulled_in": []})
        run = sphere.load_run(self.conn, run_id)
        c = run["center"]
        self.assertEqual(c["vertex"], geodesic.nearest(sphere.NORTH))
        self.assertEqual(c["relevance"][1], 1.0)                       # keys come back as ints
        st = run["stats"]
        self.assertLess(st["relevance_vs_distance"], -0.3)              # more relevant -> closer
        self.assertLess(st["top_third_mean_angle"], st["mean_angle_from_situation"])
        self.assertTrue(any(a == sphere.CENTER_ID for a, _, _ in run["edges"]))   # triangulated with the situation
        self.assertNotIn(c["vertex"], [p["vertex"] for p in run["points"]])       # no saying on its marble
        html = render.write_page(self.conn, run_id, self.dir / "c.html").read_text(encoding="utf-8")
        data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(data["center"]["text"], "a test situation")
        self.assertEqual(data["title"], "Around your situation")

    def test_consult_sphere_command(self):
        from compendium import cli, sphere
        out = self.dir / "s.html"
        code = cli.main(["--db", str(self.dir / "t.db"), "consult", "should I hesitate or commit now? the plan changed",
                         "--sphere", "--stage", "tags", "--n", "8", "--seed", "2", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        run = sphere.load_run(self.conn, self.conn.execute("SELECT max(id) FROM layout_runs").fetchone()[0])
        self.assertTrue(run["sample_how"].startswith("consult: should I hesitate"))
        self.assertTrue(set(run["center"]["pulled_in"]) <= set(run["sample"]))


if __name__ == "__main__":
    unittest.main()
