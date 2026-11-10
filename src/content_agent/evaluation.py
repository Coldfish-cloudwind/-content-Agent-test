"""Deterministic checks of exported results and optional human ratings."""
from .domain import parse_time

CRITERIA = ("topic_relevance", "asset_fidelity", "actionability")


def matches_expectation(output, expected):
    reason = output.get("reason")
    facts = {"status":output.get("status"), "cost":output.get("total_cost"),
             "variant":(output.get("script") or {}).get("variant"),
             "reason_code":reason.get("code") if isinstance(reason, dict) else None,
             "invocation":output.get("invocation"), "new_steps":output.get("new_steps")}
    checks = [facts.get(key) == value for key, value in expected.items() if key != "observations"]
    observations = [h["observation"] for h in output.get("trace", [])]
    for pattern in expected.get("observations", []):
        checks.append(any(all(row.get(key) == value for key, value in pattern.items())
                          for row in observations))
    return all(checks)


def assess(request, output):
    charges = output.get("charges", [])
    budget = request.get("budget")
    total = output.get("total_cost")
    trace = output.get("trace", [])
    plan = output.get("saved_plan")
    completed = output.get("status") == "completed"
    checks = {
        "ledger_total": type(total) is int and total == sum(c["units"] for c in charges),
        "nonnegative_charges": all(type(c["units"]) is int and c["units"] >= 0 for c in charges),
        "budget_respected": type(budget) is not int or total <= budget,
        "trace_sequence": [h["step"] for h in trace] == list(range(1, len(trace) + 1)),
        "completion_has_saved_plan": not completed or isinstance(plan, dict),
        "incomplete_has_reason": output.get("status") not in {"incomplete","error"} or bool(output.get("reason")),
    }
    if "task_plan" in output:
        steps = output["task_plan"]["steps"]
        visited = set()
        dag_valid = True
        for step in steps:
            dag_valid = dag_valid and set(step["depends_on"]) <= visited
            visited.add(step["step_id"])
        attempts = output.get("attempts",[])
        checks["plan_is_dag"] = dag_valid
        checks["attempt_costs_match_ledger"] = sum(a["result"]["cost"] for a in attempts) == total
        checks["failed_attempts_not_charged"] = all(a["result"]["cost"] == 0 for a in attempts
                                                    if a["result"]["status"] in ("failed","blocked"))
        checks["attempt_ids_unique"] = len({a["attempt_id"] for a in attempts}) == len(attempts)
        checks["step_result_references_valid"] = all(set(s["attempt_refs"]) <= {a["attempt_id"] for a in attempts}
                                                     for s in steps)
    if completed and isinstance(plan, dict):
        script = plan.get("script", {})
        assets = {a["id"] for a in output.get("assets", [])}
        beats = script.get("beats", [])
        checks["saved_total_matches"] = plan.get("total_cost") == total
        checks["assets_grounded"] = bool(beats) and all(b.get("asset") in assets for b in beats)
        checks["script_fields_present"] = all(script.get(k) for k in ("title","channel","objective","beats"))
        checks["saved_event_present"] = bool(trace) and trace[-1]["observation"].get("code") == "saved"
        schedule = plan.get("production_schedule", [])
        checks["schedule_matches_ledger"] = len(schedule) == len(charges) and all(
            all(task.get(k) == charge.get(k) for k in ("tool","units","start","finish","minutes"))
            and task.get("owner") for task, charge in zip(schedule, charges))
        try:
            previous = parse_time(output["timeline"])
            for charge in charges:
                start, finish = parse_time(charge["start"]), parse_time(charge["finish"])
                if start < previous or finish < start:
                    raise ValueError("invalid schedule")
                if (finish-start).total_seconds() != charge["minutes"]*60:
                    raise ValueError("duration mismatch")
                previous = finish
            checks["deadline_respected"] = bool(charges) and previous <= parse_time(request["deadline"])
        except (ValueError, KeyError, TypeError):
            checks["deadline_respected"] = False
    return {"checks":checks, "all_checks_passed":all(checks.values())}


def review_summary(review):
    if (not isinstance(review, dict) or not isinstance(review.get("scores"), dict)
            or set(review["scores"]) != set(CRITERIA)):
        raise ValueError("评分文件须包含全部三个评分维度")
    scores = review["scores"]
    if any(v is not None and (type(v) is not int or not 0 <= v <= 4) for v in scores.values()):
        raise ValueError("每项评分须为 0–4 整数或 null")
    if any(v is None for v in scores.values()):
        return {"status":"pending", "mean_score":None}
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        raise ValueError("完整评分须注明评审人")
    if not isinstance(review.get("notes"), str) or not review["notes"].strip():
        raise ValueError("完整评分须附具体依据")
    return {"status":"rated", "mean_score":sum(scores.values())/len(CRITERIA)}


def review_template(case_id):
    return {"case_id":case_id, "reviewer":None, "scores":dict.fromkeys(CRITERIA),
            "notes":None}
