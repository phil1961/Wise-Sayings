"""Run with:  python -m unittest discover tests"""

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


if __name__ == "__main__":
    unittest.main()
