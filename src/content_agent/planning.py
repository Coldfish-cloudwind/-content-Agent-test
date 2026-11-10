"""Versioned DAG plans and immutable attempts, committed with business state."""
from copy import deepcopy

from .contracts import envelope, price, tool_key
from .domain import CATALOG, TOOLS, spent

ORDER = ("search_assets","estimate_cost","generate_script","save_plan")
DEPS = {"search_assets":[], "estimate_cost":["search_assets"],
        "generate_script":["search_assets","estimate_cost"],
        "save_plan":["estimate_cost","generate_script"]}


def node(plan, action):
    return next(s for s in plan["steps"] if s["tool"] == action)


def refresh(state):
    plan = state["plans"][-1]
    variant = plan["variant"]
    for step in plan["steps"]:
        action = step["tool"]
        attempts = [a for a in state["attempts"] if a["action"] == action
                    and (action not in ("estimate_cost","generate_script")
                         or a["args"].get("variant") == variant)]
        step["attempt_refs"] = [a["attempt_id"] for a in attempts]
        step["actual_cost"] = sum(a["result"]["cost"] for a in attempts)
        step["actual_duration"] = sum(a["result"]["duration"] for a in attempts)
        step["result"] = deepcopy(attempts[-1]["result"]) if attempts else None
        if attempts:
            step["status"] = attempts[-1]["result"]["status"]
        else:
            step["status"] = "pending"
        step["blocked_by"] = []
        if step["status"] in ("pending","blocked"):
            for dependency in DEPS[action]:
                parent = node(plan,dependency)
                acceptable = parent["status"] == "success" or (
                    dependency == "search_assets" and bool(state["assets"])
                    and parent["status"] == "partial_success")
                if not acceptable:
                    step["blocked_by"].append(parent["step_id"])
            if step["blocked_by"]:
                step["status"] = "blocked"
            elif step["status"] == "blocked" and step["result"]:
                step["blocked_by"].append(step["result"]["code"])
        if variant in state["quotes"] and not state["quotes"][variant]["feasible"]:
            if action in ("generate_script","save_plan") and step["status"] in ("pending","blocked"):
                step["status"] = "blocked"
                step["blocked_by"].append("constraints_exceeded")
        step["reused"] = bool(attempts) and attempts[-1]["plan_version"] != plan["version"]
    plan["actual_cost"] = spent(state)
    quote = state["quotes"].get(variant)
    plan["estimated_total_cost"] = quote["total"] if quote else None
    plan["estimated_finish"] = quote["finish"] if quote else None


def new_plan(state, variant, trigger, kind):
    previous = state["plans"][-1] if state["plans"] else None
    version = len(state["plans"])+1
    if previous and previous["status"] not in ("rejected","completed"):
        previous["status"] = "abandoned"
    steps = []
    for action in ORDER:
        args = {"variant":variant} if action in ("estimate_cost","generate_script") else {}
        inputs = {"topic":state["request"].get("topic")} if action == "search_assets" else args
        estimate = price(action,variant)
        steps.append({"step_id":f"p{version}.{action}","tool":action,"inputs":inputs,
                      "depends_on":[f"p{version}.{d}" for d in DEPS[action]],
                      "input_refs":[f"p{version}.{d}.output" for d in DEPS[action]],
                      "expected_output":action+"_result","estimated_cost":estimate["units"],
                      "estimated_duration":estimate["minutes"],"critical":True,
                      "idempotency_key":tool_key(state,action,args)})
    plan = {"version":version,"kind":kind,"variant":variant,"status":"active",
            "created_after_step":len(state.get("history",[])),"trigger":deepcopy(trigger),
            "steps":steps}
    state["plans"].append(plan)
    refresh(state)
    if previous:
        state["replanning"].append({
            "from_version":previous["version"],"to_version":version,
            "trigger":deepcopy(trigger),
            "changes":{"variant":{"old":previous["variant"],"new":variant},
                       "reused_steps":[s["tool"] for s in plan["steps"] if s["reused"]],
                       "retry_steps":[s["tool"] for s in plan["steps"] if s["status"] in ("failed","partial_success")]}})
    return plan


def begin_action(state, action, args, reason):
    if action not in ORDER:
        return
    plan = state["plans"][-1]
    requested = args.get("variant")
    variant = requested if isinstance(requested,str) and requested in CATALOG["variants"] else plan["variant"]
    if state["script"]:
        variant = state["script"]["variant"]
    previous = state["history"][-1]["observation"] if state["history"] else {}
    trigger = {"code":previous.get("code","model_selected"),"model_reason":reason,
               "observation":deepcopy(previous)}
    if previous.get("budget_gap",0):
        trigger["category"] = "budget_exceeded"
    elif previous.get("late_minutes",0):
        trigger["category"] = "deadline_exceeded"
    elif previous.get("status") == "partial_success":
        trigger["category"] = "partial_success"
    elif previous.get("ok") is False:
        trigger["category"] = "tool_error"
    else:
        trigger["category"] = "model_selection_or_resume"
    if isinstance(variant,str) and variant in CATALOG["variants"] and variant != plan["variant"]:
        kind = "initial" if not any(p["variant"] for p in state["plans"]) else "alternative"
        plan = new_plan(state,variant,trigger,kind)
    elif node(plan,action)["status"] in ("failed","partial_success") or plan["status"] == "incomplete":
        plan = new_plan(state,variant,trigger,"retry")
    current = node(plan,action)
    current["inputs"] = {"topic":state["request"].get("topic"),**args} if action == "search_assets" else deepcopy(args)
    current["idempotency_key"] = tool_key(state,action,args)
    current["prior_status"] = current["status"]
    current["status"] = "retrying" if plan["kind"] == "retry" else "running"


def finish_action(state, action, args, observation, run_id):
    if action not in ORDER:
        return
    plan = state["plans"][-1]
    step = node(plan,action)
    attempt_id = f"{state['request']['campaign_id']}.attempt-{len(state['attempts'])+1}"
    observation.update(step_id=step["step_id"],attempt_id=attempt_id)
    if "status" not in observation:
        observation.update(envelope(state,action,args,observation))
    attempt = {"attempt_id":attempt_id,"plan_version":plan["version"],"step_id":step["step_id"],
               "run_id":run_id,"session":state["session"],"action":action,"args":deepcopy(args),
               "transitions":[step.get("prior_status","pending"),
                              "retrying" if plan["kind"] == "retry" else "running",observation["status"]],
               "idempotency_key":tool_key(state,action,args),"result":deepcopy(observation)}
    state["attempts"].append(attempt)
    refresh(state)
    if action == "estimate_cost" and observation["ok"] and not observation.get("feasible",True):
        plan["status"] = "rejected"


def snapshot(state):
    plan = state["plans"][-1]
    if state["status"] == "completed":
        plan["status"] = "completed"
    elif state["status"] == "incomplete" and plan["status"] != "rejected":
        plan["status"] = "incomplete"
    state["snapshots"].append({"after_step":len(state["history"]),"session":state["session"],
                              "plan_version":plan["version"],"status":state["status"],
                              "spent":spent(state),"asset_ids":[a["id"] for a in state["assets"]],
                              "has_script":bool(state["script"]),"has_saved_plan":bool(state["saved_plan"]),
                              "steps":{s["tool"]:s["status"] for s in plan["steps"]}})


def ensure_audit(state):
    state.setdefault("asset_failures",[])
    if "plans" in state:
        return
    history = state["history"]
    state.update(plans=[],attempts=[],replanning=[],snapshots=[])
    new_plan(state,None,{"code":"task_received"},"initial")
    if not history:
        return
    # Reconstruct only from persisted observations and charges; never run tools here.
    progress = {**state,"history":[],"assets":[],"quotes":{},"script":None,"saved_plan":None,
                "ledger":[],"plans":[],"attempts":[],"replanning":[],"snapshots":[],"status":"running"}
    new_plan(progress,None,{"code":"legacy_import"},"initial")
    charges = iter(state["ledger"])
    for event in history:
        progress["session"] = event.get("session",1)
        action, args = event["action"],event["args"]
        begin_action(progress,action,args,event["reason"])
        raw = deepcopy(event["observation"])
        cost = duration = 0
        if raw["code"] in ("assets_found","script_ready","saved"):
            charge = next(charges,None)
            if charge:
                progress["ledger"].append(charge)
                cost,duration = charge["units"],charge["minutes"]
        if action == "search_assets" and raw["ok"]:
            progress["assets"] = raw.get("assets",state["assets"])
        if action == "estimate_cost" and raw["ok"]:
            progress["quotes"][args["variant"]] = raw
        if raw["code"] == "script_ready":
            progress["script"] = state["script"]
        if raw["code"] == "saved":
            progress["saved_plan"] = state["saved_plan"]
            progress["status"] = "completed"
        if action in ORDER:
            normalized = envelope(progress,action,args,raw,cost,duration)
            finish_action(progress,action,args,normalized,"legacy-import")
        progress["history"].append(event)
        snapshot(progress)
    for key in ("plans","attempts","replanning","snapshots"):
        state[key] = progress[key]
    state["audit_migrated"] = True
    refresh(state)


def unfinished(state):
    if state["status"] == "completed":
        return None
    plan = state["plans"][-1]
    quote = state["quotes"].get(plan["variant"],{})
    failed = next((s["tool"] for s in plan["steps"] if s["status"] in ("failed","partial_success")),None)
    pending = next((s["tool"] for s in plan["steps"] if s["status"] == "pending"),None)
    blocked = next((s["tool"] for s in plan["steps"] if s["status"] == "blocked"),None)
    last = state["history"][-1]["observation"] if state["history"] else {}
    gaps = quote or last
    missing_fields = (state.get("reason") or {}).get("missing_fields",[])
    no_assets = any(a["result"]["code"] == "no_assets" for a in state["attempts"])
    missing_assets = state["asset_failures"] or (["matching_asset"] if no_assets and not state["assets"] else [])
    advice = []
    if missing_fields:
        advice.append("补齐字段："+", ".join(missing_fields)+"，然后重新执行。")
    if gaps.get("budget_gap",0):
        advice.append(f"增加 {gaps['budget_gap']} 个模拟单位，或补充更低成本版本后创建新任务。")
    if gaps.get("late_minutes",0):
        advice.append(f"至少延长 {gaps['late_minutes']} 分钟，或缩减制作范围后创建新任务。")
    if missing_assets:
        advice.append("补充匹配素材，或对 failed_asset_ids 使用按 ID 重试。")
    if not advice:
        advice.append("检查失败记录；可恢复错误或步数上限使用 --resume 继续。")
    return {"stopped_at":failed or pending or blocked,"budget_gap":gaps.get("budget_gap",0),
            "late_minutes":gaps.get("late_minutes",0),
            "missing_fields":missing_fields,"missing_assets":missing_assets,
            "retained_steps":[s["tool"] for s in plan["steps"] if s["status"] in ("success","partial_success")],
            "next_steps":advice}
