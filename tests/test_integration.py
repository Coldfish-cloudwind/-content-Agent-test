"""Run real subprocesses and local HTTP traffic, with deterministic model responses."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.content_agent.__main__ import write_output
from src.content_agent.domain import ROOT
from src.content_agent.models import mock
from tests.http_fixture import model_endpoint

NOW = "2030-05-01T09:00:00+08:00"


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/".runtime")
        self.folder = Path(self.tmp.name)
        self.db = self.folder/"db.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, name="replan", *flags):
        return [sys.executable,"-m","src.content_agent",f"examples/inputs/{name}.json",
                "--as-of",NOW,"--db",str(self.db),*flags]

    def invoke(self, args, env=None):
        p = subprocess.run(args, cwd=ROOT, capture_output=True, encoding="utf-8",
                           env=env, timeout=20)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        return p, json.loads(p.stdout)

    def test_http_replanning_receives_actual_observations(self):
        with model_endpoint() as (url, requests):
            env = {**os.environ,"PLANNER_MODEL":"http-fixture","PLANNER_BASE_URL":url,
                   "PLANNER_API_KEY":""}
            p, r = self.invoke(self.command("replan","--model","api"), env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["total_cost"], 8)
        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[2]["incoming_observation"]["history"][-1]["observation"]["budget_gap"], 2)
        self.assertEqual(requests[2]["fixture_decision"]["args"], {"variant":"lite"})

    def test_http_tool_failure_keeps_assets_and_replans(self):
        with model_endpoint() as (url, requests):
            env = {**os.environ,"PLANNER_MODEL":"http-fixture","PLANNER_BASE_URL":url,
                   "PLANNER_API_KEY":""}
            p, r = self.invoke(self.command("failure","--model","api","--fail-once","generate_script"), env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["total_cost"], 8)
        self.assertFalse(requests[3]["incoming_observation"]["history"][-1]["observation"]["ok"])
        self.assertEqual(len(r["assets"]), 2)

    def test_http_faults_return_incomplete_with_success_retained(self):
        for fault in ("disconnect", "invalid_body", "http_503"):
            with model_endpoint(fault) as (url, requests):
                env = {**os.environ,"PLANNER_MODEL":"http-fixture","PLANNER_BASE_URL":url,
                       "PLANNER_API_KEY":""}
                with self.subTest(fault=fault):
                    p, r = self.invoke(self.command("replan","--model","api",
                                                   "--db",str(self.folder/f"{fault}.sqlite")), env)
                    self.assertEqual(p.returncode, 2)
                    self.assertEqual(r["reason"]["code"], "model_error")
                    self.assertEqual(r["total_cost"], 1)
                    self.assertEqual(len(r["assets"]), 2)
                    self.assertEqual(r["steps"]["search_assets"], "succeeded")

    def test_extreme_clock_returns_json_error(self):
        p, r = self.invoke(self.command("normal","--as-of","0001-01-01T00:00:00+14:00"))
        self.assertEqual(p.returncode, 3)
        self.assertEqual(r["status"], "error")

    def test_output_cannot_overwrite_request_or_database(self):
        source = self.folder/"input.json"
        original = (ROOT/"examples/inputs/normal.json").read_text(encoding="utf-8")
        source.write_text(original, encoding="utf-8")
        cmd = self.command("normal")
        cmd[3] = str(source)
        p, _ = self.invoke([*cmd,"--out",str(source)])
        self.assertEqual(p.returncode, 3)
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.invoke(cmd)
        before = self.db.read_bytes()
        p, _ = self.invoke([*cmd,"--out",str(self.db)])
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.db.read_bytes(), before)

    def test_failed_export_preserves_previous_file(self):
        path = self.folder/"result.json"
        path.write_text("old complete export", encoding="utf-8")
        with patch("src.content_agent.__main__.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                write_output(path, "new export")
        self.assertEqual(path.read_text(encoding="utf-8"), "old complete export")
        self.assertEqual(list(self.folder.glob(".export-*.tmp")), [])

    def test_concurrent_cli_calls_do_not_duplicate_costs(self):
        processes = [subprocess.Popen(self.command("normal"), cwd=ROOT, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, encoding="utf-8") for _ in range(4)]
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            self.assertIn(proc.returncode, (0,3), stdout+stderr)
            self.assertNotIn("Traceback", stderr)
        p, r = self.invoke(self.command("normal"))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["total_cost"], 12)
        self.assertEqual(len(r["charges"]), 3)
        self.assertEqual(len(r["trace"]), 4)

    def bridge_run(self, name, *flags):
        response = self.folder/"decision.json"
        envelope_flags = []
        for _ in range(10):
            p, r = self.invoke(self.command(name,"--model","bridge",*flags,*envelope_flags))
            if r["status"] != "running":
                return p, r
            ctx = r["model_request"]["observation"]
            response.write_text(json.dumps({"campaign_id":name,"revision":r["revision"],
                                           "decision":mock(ctx)}, ensure_ascii=False),encoding="utf-8")
            envelope_flags = ["--decision-file",str(response)]
        self.fail("bridge did not terminate")

    def test_cli_bridge_budget_observe_act_loop(self):
        p, r = self.bridge_run("replan")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["total_cost"], 8)
        self.assertEqual(r["trace"][1]["observation"]["budget_gap"], 2)
        self.assertEqual(r["trace"][2]["args"], {"variant":"lite"})

    def test_cli_bridge_failed_tool_observe_act_loop(self):
        p, r = self.bridge_run("failure","--fail-once","generate_script")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["total_cost"], 8)
        self.assertFalse(r["trace"][2]["observation"]["ok"])
        self.assertEqual(r["trace"][3]["args"], {"variant":"lite"})
