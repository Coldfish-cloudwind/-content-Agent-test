"""Business invariants and failure recovery through the same engine as the CLI."""
import copy
import io
from http.client import IncompleteRead
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from src.content_agent.domain import ROOT, CATALOG, initial, parse_time
from src.content_agent.engine import run
from src.content_agent.models import ModelError, api_model, decision
from src.content_agent.store import Conflict, Store
from src.content_agent.tools import execute

NOW = "2030-05-01T09:00:00+08:00"


class AgentTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/".runtime")
        self.db = str(Path(self.tmp.name)/"state.sqlite")
        self.store = Store(self.db)
        self.req = dict(campaign_id="test", topic="校园迎新", budget=15,
                        deadline="2030-05-01T10:00:00+08:00")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_agent(self, **kwargs):
        return run(self.store, self.req, lambda: NOW, **kwargs)

    def test_normal_and_accounting(self):
        r = self.run_agent()
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["total_cost"], 12)
        self.assertEqual(sum(c["units"] for c in r["charges"]), 12)
        self.assertEqual(r["saved_plan"]["total_cost"], 12)
        self.assertTrue(all(s == "succeeded" for s in r["steps"].values()))
        for a, b in zip(r["charges"], r["charges"][1:]):
            self.assertLessEqual(parse_time(a["finish"]), parse_time(b["start"]))
        self.assertLessEqual(parse_time(r["charges"][-1]["finish"]), parse_time(self.req["deadline"]))

    def test_budget_replanning(self):
        self.req["budget"] = 10
        r = self.run_agent()
        self.assertEqual(r["quotes"]["studio"]["budget_gap"], 2)
        self.assertEqual(r["saved_plan"]["script"]["variant"], "lite")
        self.assertEqual(r["total_cost"], 8)

    def test_missing_fields_no_side_effects(self):
        for field in ("budget", "deadline", "topic"):
            req = {k:v for k,v in self.req.items() if k != field}
            r = run(self.store, req, lambda:NOW)
            self.assertEqual(r["reason"]["code"], "invalid_input")
            self.assertEqual(r["trace"], [])
            self.assertIsNone(self.store.get("test"))

    def test_no_feasible_plan(self):
        self.req["budget"] = 6
        r = self.run_agent()
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["saved_plan"])
        self.assertEqual(r["quotes"]["lite"]["budget_gap"], 2)
        self.assertEqual(r["total_cost"], 1)

    def test_tool_failure_switches_variant_keeps_assets(self):
        r = self.run_agent(fail_once="generate_script")
        failed = [h for h in r["trace"] if not h["observation"]["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["spent_after"], 1)
        self.assertEqual(r["saved_plan"]["script"]["variant"], "lite")
        self.assertEqual(sum(h["action"] == "search_assets" for h in r["trace"]), 1)

    def test_repeat_no_duplicate_steps_or_charges(self):
        a = self.run_agent()
        b = self.run_agent()
        self.assertEqual(b["invocation"], "cached")
        self.assertEqual(a["trace"], b["trace"])
        self.assertEqual(a["charges"], b["charges"])

    def test_id_reuse_conflict(self):
        self.run_agent()
        self.req["topic"] = "咖啡市集"
        with self.assertRaises(Conflict):
            self.run_agent()

    def test_resume_retains_script_and_failed_event(self):
        a = self.run_agent(limit=4, fail_once="save_plan")
        self.assertEqual(a["status"], "incomplete")
        self.assertEqual(a["steps"]["save_plan"], "failed")
        self.assertEqual(a["total_cost"], 10)
        b = self.run_agent(resume=True, fail_once="save_plan")
        self.assertEqual(b["status"], "completed")
        self.assertEqual(b["new_steps"], 1)
        self.assertEqual(b["script"], a["script"])
        self.assertEqual(b["total_cost"], 12)
        self.assertFalse(b["trace"][3]["observation"]["ok"])

    def test_deadline_replanning(self):
        self.req["deadline"] = "2030-05-01T09:20:00+08:00"
        r = self.run_agent()
        self.assertEqual(r["quotes"]["studio"]["late_minutes"], 18)
        self.assertEqual(r["total_cost"], 8)

    def test_expired_request(self):
        self.req["deadline"] = "2030-05-01T08:00:00+08:00"
        r = self.run_agent()
        self.assertEqual(r["reason"]["code"], "deadline_passed")
        self.assertEqual(r["total_cost"], 0)

    def test_no_assets(self):
        self.req["topic"] = "深海珊瑚"
        r = self.run_agent()
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["steps"]["search_assets"], "failed")

    def test_partial_assets(self):
        self.req["topic"] = "旧书交换"
        r = self.run_agent()
        self.assertNotIn("studio", r["quotes"])
        self.assertEqual(r["saved_plan"]["script"]["variant"], "lite")

    def test_invalid_requests(self):
        for value in (True, 1.5, -1, "10"):
            req = {**self.req, "budget":value}
            self.assertEqual(run(self.store, req, lambda:NOW)["reason"]["code"], "invalid_input")
        for key in ("../bad", "", "a/b"):
            with self.assertRaises(ValueError):
                run(self.store, {**self.req, "campaign_id":key}, lambda:NOW)

    def test_step_and_lifetime_bounds(self):
        repeat = decision("search_assets", "重复查询以验证上限")
        with patch("src.content_agent.engine.mock", return_value=repeat):
            r = self.run_agent(limit=2)
            self.assertEqual(len(r["trace"]), 2)
            self.assertEqual(r["reason"]["code"], "step_limit")
            for _ in range(3):
                r = self.run_agent(limit=10, resume=True)
        self.assertEqual(len(r["trace"]), 32)
        self.assertEqual(r["reason"]["code"], "lifetime_limit")
        self.assertEqual(r["total_cost"], 1)

    def test_invalid_model_response_returns_incomplete(self):
        with patch("src.content_agent.engine.api_model", return_value={"action":"bad"}):
            r = self.run_agent(mode="api")
        self.assertEqual(r["reason"]["code"], "model_error")
        self.assertEqual(r["total_cost"], 0)

    def test_bridge_revision_rejects_stale_action(self):
        a = self.run_agent(mode="bridge")
        envelope = {"campaign_id":"test", "revision":a["revision"],
                    "decision":decision("search_assets", "查素材")}
        b = self.run_agent(mode="bridge", envelope=envelope)
        self.assertEqual(b["revision"], 1)
        with self.assertRaises(Conflict):
            self.run_agent(mode="bridge", envelope=envelope)

    def test_concurrent_state_commit_rejects_duplicate_charge(self):
        self.run_agent(mode="bridge")
        a = self.store.get("test")
        b = copy.deepcopy(a)
        execute(a, "search_assets", {}, NOW)
        execute(b, "search_assets", {}, NOW)
        self.store.commit(a)
        with self.assertRaises(Conflict):
            self.store.commit(b)
        self.assertEqual(len(self.store.get("test")["ledger"]), 1)

    def test_dependency_and_budget_guards(self):
        s = initial(self.req, NOW)
        self.assertEqual(execute(s, "save_plan", {}, NOW)["code"], "search_required")
        execute(s, "search_assets", {}, NOW)
        self.assertEqual(execute(s, "generate_script", {"variant":"studio"}, NOW)["code"], "estimate_required")
        execute(s, "estimate_cost", {"variant":"studio"}, NOW)
        s["request"]["budget"] = 5
        self.assertEqual(execute(s, "generate_script", {"variant":"studio"}, NOW)["code"], "constraints_exceeded")
        self.assertIsNone(s["script"])

    def test_wall_clock_rechecked_after_model(self):
        times = iter([NOW, NOW, "2030-05-01T10:01:00+08:00"])
        with patch("src.content_agent.engine.mock", return_value=decision("search_assets", "检索")):
            r = run(self.store, self.req, lambda:next(times), limit=1)
        self.assertEqual(r["trace"][0]["observation"]["code"], "search_too_late")
        self.assertEqual(r["total_cost"], 0)

    def test_api_protocol_and_secret_not_traced(self):
        body = {"choices":[{"message":{"content":json.dumps(decision("search_assets", "查素材"))}}]}
        response = io.StringIO(json.dumps(body))
        with patch.dict(os.environ, {"PLANNER_MODEL":"test-model","PLANNER_API_KEY":"test-secret"}):
            with patch("src.content_agent.models.urlopen", return_value=response) as call:
                r = self.run_agent(mode="api", limit=1)
        req = call.call_args.args[0]
        self.assertEqual(json.loads(req.data)["model"], "test-model")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn("test-secret", json.dumps(r))

    def test_api_failure_never_falls_back(self):
        with patch("src.content_agent.engine.api_model", side_effect=ModelError("HTTP 503")):
            r = self.run_agent(mode="api")
        self.assertEqual(r["reason"]["code"], "model_error")
        self.assertIsNone(r["script"])

    def test_cli_entry_and_cached_exit(self):
        cmd = [sys.executable,"-m","src.content_agent","examples/inputs/normal.json",
               "--db",self.db,"--as-of",NOW]
        for event in ("executed","cached"):
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, encoding="utf-8")
            self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
            self.assertEqual(json.loads(p.stdout)["invocation"], event)

    def test_catalog_content_change_rejects_checkpoint(self):
        self.run_agent(mode="bridge")
        original = CATALOG["variants"]["studio"]["units"]
        with patch.dict(CATALOG["variants"]["studio"], {"units":original + 1}):
            with self.assertRaises(Conflict):
                self.run_agent()

    def test_legacy_checkpoint_can_resume(self):
        self.run_agent(mode="bridge")
        state = self.store.get("test")
        del state["catalog_signature"]
        self.store.commit(state)
        r = self.run_agent()
        self.assertEqual(r["status"], "completed")
        self.assertIn("catalog_signature", self.store.get("test"))

    def test_truncated_http_response_is_model_error(self):
        with patch.dict(os.environ, {"PLANNER_MODEL":"test-model"}):
            with patch("src.content_agent.models.urlopen", side_effect=IncompleteRead(b"", 10)):
                r = self.run_agent(mode="api")
        self.assertEqual(r["reason"]["code"], "model_error")
        self.assertIn("IncompleteRead", r["reason"]["message"])

    def test_invalid_endpoint_retains_explicit_failure(self):
        with patch.dict(os.environ, {"PLANNER_MODEL":"test", "PLANNER_BASE_URL":"https://[bad"}):
            r = self.run_agent(mode="api")
        self.assertEqual(r["reason"]["code"], "model_error")

    def test_resume_retries_only_variant_after_transient_failure(self):
        self.req["topic"] = "社区旧书交换"
        a = self.run_agent(fail_once="generate_script")
        self.assertEqual(a["status"], "incomplete")
        self.assertEqual(a["total_cost"], 1)
        b = self.run_agent(resume=True, fail_once="generate_script")
        self.assertEqual(b["status"], "completed")
        self.assertEqual(b["total_cost"], 8)
        self.assertEqual(b["new_steps"], 2)
        self.assertEqual(sum(h["action"] == "search_assets" for h in b["trace"]), 1)


if __name__ == "__main__":
    unittest.main()
