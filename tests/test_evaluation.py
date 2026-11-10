"""Protect evaluation metrics and prevent unreviewed content receiving human scores."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.content_agent.domain import ROOT
from src.content_agent.evaluation import assess, matches_expectation, review_summary, review_template


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime").mkdir(exist_ok=True)
        self.request = json.loads((ROOT/"examples/inputs/normal.json").read_text(encoding="utf-8"))
        self.output = json.loads((ROOT/"examples/observed/normal.json").read_text(encoding="utf-8"))

    def test_valid_result_passes_structural_checks(self):
        self.assertTrue(assess(self.request,self.output)["all_checks_passed"])

    def test_model_failure_cannot_pass_no_assets_expectation(self):
        expected = {"status":"incomplete", "cost":0, "reason_code":"model_stopped",
                    "observations":[{"code":"no_assets"}]}
        wrong = {"status":"incomplete", "total_cost":0,
                 "reason":{"code":"model_error"}, "trace":[]}
        self.assertFalse(matches_expectation(wrong,expected))
        self.assertFalse(matches_expectation({"status":"error","reason":"storage unavailable"},expected))

    def test_corrupted_cost_assets_and_schedule_are_detected(self):
        for mutation, field in (("cost","ledger_total"),("asset","assets_grounded"),("time","deadline_respected")):
            result = copy.deepcopy(self.output)
            if mutation == "cost":
                result["total_cost"] = 999
            elif mutation == "asset":
                result["saved_plan"]["script"]["beats"][0]["asset"] = "missing"
            else:
                result["charges"][-1]["finish"] = "2030-05-02T09:00:00+08:00"
            self.assertFalse(assess(self.request,result)["checks"][field])
        result = {**self.output,"saved_plan":None}
        self.assertFalse(assess(self.request,result)["all_checks_passed"])

    def test_pending_review_has_no_score(self):
        result = review_summary(review_template("normal"))
        self.assertEqual(result,{"status":"pending","mean_score":None})

    def test_review_validation_and_explicit_zero(self):
        review = review_template("normal")
        review.update(reviewer="test-reviewer",notes="test input for scorer validation")
        review["scores"] = dict.fromkeys(review["scores"],0)
        self.assertEqual(review_summary(review)["mean_score"],0)
        for invalid in (True,5,-1,"3"):
            bad = copy.deepcopy(review)
            bad["scores"]["topic_relevance"] = invalid
            with self.assertRaises(ValueError):
                review_summary(bad)
        for invalid in (None,[],{"scores":None}):
            with self.assertRaises(ValueError):
                review_summary(invalid)

    def test_batch_cli_expectations_and_negative_case_denominator(self):
        with tempfile.TemporaryDirectory(dir=ROOT/".runtime") as tmp:
            proc = subprocess.run([sys.executable,"-m","examples.evaluate","--out",tmp],
                                  cwd=ROOT,capture_output=True,encoding="utf-8",timeout=60)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["cases"],18)
            self.assertEqual(result["expectation_matches"],18)
            self.assertEqual(result["structural_checks_passed"],18)
            self.assertEqual(result["completion_count"],10)
            reviews = json.loads((Path(tmp)/"human-review.json").read_text(encoding="utf-8"))
            self.assertTrue(all(review_summary(r)["status"] == "pending" for r in reviews))
