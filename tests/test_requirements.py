"""Direct acceptance checks for the user's twelve requirement categories."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.content_agent.domain import CATALOG,ROOT
from src.content_agent.engine import run
from src.content_agent.models import decision
from src.content_agent.store import Store
from src.content_agent.tools import execute

NOW = "2030-05-01T09:00:00+08:00"


class RequirementTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/".runtime")
        self.folder = Path(self.tmp.name)
        self.store = Store(self.folder/"db.sqlite")
        self.request = {"campaign_id":"audit","topic":"校园迎新","budget":15,
                        "deadline":"2030-05-01T10:00:00+08:00"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_agent(self, **kwargs):
        return run(self.store,self.request,lambda:NOW,**kwargs)

    def test_missing_fields_are_structured_and_all_steps_blocked(self):
        del self.request["budget"]
        r = self.run_agent()
        self.assertEqual(r["reason"]["missing_fields"],["budget"])
        self.assertEqual(r["unfinished"]["missing_fields"],["budget"])
        self.assertTrue(all(s["status"] == "blocked" for s in r["task_plan"]["steps"]))
        self.assertEqual(r["attempts"],[])

    def test_dag_contracts_accounting_and_snapshots(self):
        r = self.run_agent()
        plan = r["task_plan"]
        visited = set()
        for step in plan["steps"]:
            self.assertTrue(set(step["depends_on"]) <= visited)
            visited.add(step["step_id"])
            for key in ("inputs","expected_output","estimated_cost","estimated_duration",
                        "actual_cost","actual_duration","idempotency_key","result"):
                self.assertIn(key,step)
        for attempt in r["attempts"]:
            observation = attempt["result"]
            for key in ("step_id","status","cost","duration","output","error","idempotency_key"):
                self.assertIn(key,observation)
        self.assertEqual(sum(a["result"]["cost"] for a in r["attempts"]),r["total_cost"])
        self.assertEqual(len(r["snapshots"]),len(r["trace"]))
        self.assertTrue(r["snapshots"][-1]["has_saved_plan"])

    def test_rejected_plan_and_reused_results_preserved(self):
        self.request["budget"] = 10
        r = self.run_agent()
        studio = next(p for p in r["plan_versions"] if p["variant"] == "studio")
        self.assertEqual(studio["status"],"rejected")
        self.assertEqual(studio["estimated_total_cost"],12)
        self.assertEqual(r["task_plan"]["estimated_total_cost"],8)
        self.assertEqual(r["task_plan"]["variant"],"lite")
        self.assertIn("search_assets",r["replanning"][-1]["changes"]["reused_steps"])
        self.assertEqual(sum(a["action"] == "search_assets" for a in r["attempts"]),1)

    def test_failed_attempt_never_changes_to_success(self):
        r = self.run_agent(fail_once="generate_script")
        failed = [a for a in r["attempts"] if a["result"]["status"] == "failed"]
        self.assertEqual(len(failed),1)
        self.assertEqual(failed[0]["result"]["cost"],0)
        old = next(p for p in r["plan_versions"] if p["version"] == failed[0]["plan_version"])
        self.assertEqual(next(s for s in old["steps"] if s["tool"] == "generate_script")["status"],"failed")
        self.assertEqual(r["status"],"completed")

    def test_partial_success_retries_only_failed_item(self):
        r = self.run_agent(partial_once=True)
        first, second = r["attempts"][:2]
        self.assertEqual(first["result"]["status"],"partial_success")
        self.assertEqual(first["result"]["failed_asset_ids"],["C02"])
        self.assertEqual(second["args"],{"asset_ids":["C02"]})
        self.assertEqual(second["result"]["retrieved_asset_ids"],["C02"])
        self.assertEqual(second["result"]["cost"],0)
        self.assertEqual(r["total_cost"],12)
        self.assertEqual(len({a["id"] for a in r["assets"]}),2)
        repeated = execute(self.store.get("audit"),"search_assets",{"asset_ids":["C02"]},NOW)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["cost"],0)

    def test_dependency_failure_blocks_actual_save(self):
        commands = [decision("search_assets","检索"),
                    decision("save_plan","验证依赖守卫"),decision("stop","依赖失败")]
        with patch("src.content_agent.engine.api_model",side_effect=commands):
            r = self.run_agent(mode="api",fail_once="search_assets")
        self.assertIsNone(r["saved_plan"])
        self.assertEqual(r["total_cost"],0)
        self.assertEqual(r["attempts"][1]["result"]["status"],"blocked")
        statuses = {s["tool"]:s["status"] for s in r["task_plan"]["steps"]}
        self.assertEqual(statuses["search_assets"],"failed")
        self.assertEqual(statuses["generate_script"],"blocked")
        self.assertEqual(statuses["save_plan"],"blocked")

    def test_run_ids_and_step_keys_with_cached_result(self):
        a = self.run_agent()
        b = self.run_agent()
        self.assertNotEqual(a["run_id"],b["run_id"])
        self.assertEqual(a["task_id"],b["task_id"])
        self.assertEqual(a["attempts"],b["attempts"])
        self.assertEqual(b["new_steps"],0)
        self.assertEqual(b["invocation"],"cached")

    def test_direct_save_is_idempotent(self):
        self.run_agent()
        state = self.store.get("audit")
        before = copy.deepcopy(state["ledger"])
        r = execute(state,"save_plan",{},NOW)
        self.assertEqual(r["code"],"reused_plan")
        self.assertTrue(r["reused"])
        self.assertEqual(r["cost"],0)
        self.assertEqual(state["ledger"],before)

    def test_resume_preserves_old_failure_and_adds_two_attempts(self):
        self.request["topic"] = "旧书交换"
        a = self.run_agent(fail_once="generate_script")
        failed = copy.deepcopy([x for x in a["attempts"] if x["result"]["status"] == "failed"])
        b = self.run_agent(resume=True,fail_once="generate_script")
        self.assertEqual(b["status"],"completed")
        self.assertEqual(b["new_steps"],2)
        self.assertEqual([x for x in b["attempts"] if x["result"]["status"] == "failed"],failed)
        self.assertEqual(b["attempts"][-1]["session"],2)

    def test_legacy_checkpoint_reconstruction_preserves_charges(self):
        a = self.run_agent()
        state = self.store.get("audit")
        for key in ("plans","attempts","replanning","snapshots"):
            del state[key]
        self.store.commit(state)
        b = self.run_agent()
        self.assertEqual(b["total_cost"],a["total_cost"])
        self.assertEqual(b["charges"],a["charges"])
        self.assertEqual(len(b["attempts"]),4)
        self.assertEqual(b["task_plan"]["status"],"completed")

    def test_cli_publishes_tool_contracts(self):
        p = subprocess.run([sys.executable,"-m","src.content_agent","--describe-tools"],
                           cwd=ROOT,capture_output=True,encoding="utf-8",timeout=15)
        self.assertEqual(p.returncode,0,p.stderr)
        output = json.loads(p.stdout)
        self.assertTrue(output["tools"]["search_assets"]["partial_success"])
        self.assertIn("generate_script",output["tools"]["save_plan"]["prerequisites"])

    def test_custom_catalog_and_invalid_fee_validation(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["variants"]["studio"]["units"] = 4
        path = self.folder/"catalog.json"
        path.write_text(json.dumps(catalog,ensure_ascii=False),encoding="utf-8")
        command = [sys.executable,"-m","src.content_agent","examples/inputs/normal.json",
                   "--catalog",str(path),"--db",str(self.folder/"custom.sqlite"),"--as-of",NOW]
        p = subprocess.run(command,cwd=ROOT,capture_output=True,encoding="utf-8",timeout=15)
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        self.assertEqual(json.loads(p.stdout)["total_cost"],7)
        catalog["variants"]["studio"]["units"] = -1
        path.write_text(json.dumps(catalog),encoding="utf-8")
        p = subprocess.run(command,cwd=ROOT,capture_output=True,encoding="utf-8",timeout=15)
        self.assertEqual(p.returncode,3)
        self.assertEqual(json.loads(p.stdout)["status"],"error")

    def test_unknown_variant_is_structured_failure(self):
        commands = [decision("search_assets","检索"),
                    decision("generate_script","无效参数测试",variant=[]),
                    decision("stop","结束测试")]
        with patch("src.content_agent.engine.api_model",side_effect=commands):
            r = self.run_agent(mode="api")
        self.assertEqual(r["attempts"][1]["result"]["code"],"unknown_variant")
        self.assertEqual(r["total_cost"],1)

    def test_paid_script_cannot_be_relabelled_as_another_plan(self):
        commands = [decision("search_assets","检索"),decision("estimate_cost","估价",variant="studio"),
                    decision("generate_script","制作",variant="studio"),
                    decision("estimate_cost","无效切换",variant="lite"),decision("save_plan","保存")]
        with patch("src.content_agent.engine.api_model",side_effect=commands):
            r = self.run_agent(mode="api")
        self.assertEqual(r["task_plan"]["variant"],"studio")
        self.assertEqual(r["saved_plan"]["script"]["variant"],"studio")
        self.assertEqual(r["attempts"][3]["result"]["code"],"script_already_committed")
        self.assertEqual(r["total_cost"],12)

    def test_partial_results_can_support_explicit_degradation(self):
        commands = [decision("search_assets","检索"),decision("estimate_cost","以现有素材降级",variant="lite"),
                    decision("generate_script","制作图文",variant="lite"),decision("save_plan","保存")]
        with patch("src.content_agent.engine.api_model",side_effect=commands):
            r = self.run_agent(mode="api",partial_once=True)
        self.assertEqual(r["status"],"completed")
        self.assertEqual(r["total_cost"],8)
        self.assertEqual(r["asset_failures"],["C02"])
        self.assertEqual(r["task_plan"]["steps"][0]["status"],"partial_success")
        self.assertEqual(sum(a["action"] == "search_assets" for a in r["attempts"]),1)
