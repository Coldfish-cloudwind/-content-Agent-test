"""Run bounded observe/act cycles; all side effects remain local until commit."""
import os
from uuid import uuid4
from .domain import CATALOG, HARD_LIMIT, initial, parse_time, signature, spent, validate
from .models import SYSTEM, ModelError, api_model, check_decision, context, mock
from .store import Conflict
from .tools import execute, result
from .planning import begin_action, ensure_audit, finish_action, snapshot, unfinished


def end(state, code, message):
    state.update(status="incomplete", reason={"code":code, "message":message})


def report(state, event, new_steps=0):
    ensure_audit(state)
    if state["status"] == "incomplete" and state["plans"][-1]["status"] == "active":
        state["plans"][-1]["status"] = "incomplete"
    if (state.get("reason") or {}).get("code") == "invalid_input":
        for step in state["plans"][-1]["steps"]:
            step.update(status="blocked",blocked_by=["input_validation"])
    hist = state["history"]
    trace = [{**h, "next_action":hist[i+1]["action"] if i+1 < len(hist) else state["status"]}
             for i, h in enumerate(hist)]
    return dict(campaign_id=state["request"]["campaign_id"], status=state["status"],
                reason=state["reason"], invocation=event, new_steps=new_steps,
                revision=state["revision"], timeline=state["start"], steps=state["steps"],
                assets=state["assets"],asset_failures=state.get("asset_failures",[]), quotes=state["quotes"], script=state["script"],
                charges=state["ledger"], total_cost=spent(state), saved_plan=state["saved_plan"],
                trace=trace,task_id=state["request"]["campaign_id"],run_id=state.get("run_id"),
                task_plan=state["plans"][-1],plan_versions=state["plans"],
                replanning=state["replanning"],attempts=state["attempts"],
                snapshots=state["snapshots"],unfinished=unfinished(state))


def run(store, request, clock, mode="mock", limit=10, resume=False,
        fail_once=None, envelope=None, external_name="external",partial_once=False):
    issues = validate(request)
    if not 1 <= limit <= HARD_LIMIT:
        raise ValueError(f"max-steps 应在 1–{HARD_LIMIT} 之间")
    now = clock()
    key = request["campaign_id"]
    state = initial(request, now)
    state["run_id"] = uuid4().hex
    state["limit"] = limit
    if issues:
        end(state, "invalid_input", "；".join(issues))
        state["reason"]["missing_fields"] = [key for key in ("topic","budget","deadline")
                                             if request.get(key) is None or request.get(key) == ""]
        return report(state, "input_validation")
    state = store.create(state)
    ensure_audit(state)
    state["run_id"] = uuid4().hex
    if request != state["request"]:
        raise Conflict("同一 campaign_id 的请求内容不同，请给新需求使用新 ID。")
    if state["catalog_version"] != CATALOG["version"]:
        raise Conflict("检查点费率版本不同，请新建活动。")
    catalog_signature = signature(CATALOG)
    if state.get("catalog_signature", catalog_signature) != catalog_signature:
        raise Conflict("素材或费率内容已改变，请新建活动，避免混用旧报价。")
    # Legacy checkpoints adopt the current catalog on their next successful commit.
    state["catalog_signature"] = catalog_signature
    if envelope is not None and (not isinstance(envelope, dict) or
            envelope.get("campaign_id") != key or type(envelope.get("revision")) is not int or
            envelope["revision"] != state["revision"]):
        raise Conflict("外部动作的 campaign_id 或 revision 与检查点不一致。")
    if state["status"] == "completed":
        return report(state, "cached")
    if state["status"] == "incomplete":
        if not resume or len(state["history"]) >= HARD_LIMIT:
            return report(state, "cached")
        state.update(session=state["session"]+1, used=0, limit=limit, reason=None, status="running")
        store.commit(state)
    if parse_time(now) > parse_time(request["deadline"]):
        end(state, "deadline_passed", "截止时间已过。")
        snapshot(state)
        store.commit(state)
        return report(state, "deadline_check")
    count = 0
    while state["status"] == "running":
        now = clock()
        ctx = context(state, now)
        if mode == "bridge" and envelope is None:
            return {**report(state, "awaiting_decision", count),
                    "model_request":{"system":SYSTEM, "observation":ctx,
                                     "reply_envelope":{"campaign_id":key, "revision":state["revision"],
                                                       "decision":{"action":"…", "args":{}, "reason":"…"}}}}
        source = mode
        try:
            if mode == "api":
                source = "api/" + os.environ.get("PLANNER_MODEL", "(unset)")
                d = api_model(ctx)
            elif mode == "bridge":
                source = "bridge/" + external_name
                d = envelope.get("decision")
                envelope = None
            else:
                d = mock(ctx)
            check_decision(d)
        except ModelError as exc:
            end(state, "model_error", str(exc))
            d = dict(action="model_error", args={}, reason=str(exc))
            obs = result(False, "model_error", message=str(exc))
        else:
            begin_action(state,d["action"],d["args"],d["reason"])
            if d["action"] == "stop":
                end(state, "model_stopped", d["reason"])
                obs = result(True, "stopped")
            else:
                # Re-read wall time after inference. The model cannot override the guard.
                obs = execute(state, d["action"], d["args"], clock(), fail_once,partial_once)
                finish_action(state,d["action"],d["args"],obs,state["run_id"])
                if obs["ok"]:
                    state["steps"][d["action"]] = "partial_success" if obs["status"] == "partial_success" else "succeeded"
                elif state["steps"][d["action"]] != "succeeded":
                    state["steps"][d["action"]] = "failed"
                if obs["code"] == "saved":
                    state.update(status="completed", reason=None)
        state["used"] += 1
        count += 1
        state["history"].append(dict(step=len(state["history"])+1, session=state["session"],
                                     run_id=state["run_id"],model=source, **d, observation=obs, spent_after=spent(state)))
        if state["status"] == "running":
            if len(state["history"]) >= HARD_LIMIT:
                end(state, "lifetime_limit", "累计决策达到 32 步。")
            elif state["used"] >= state["limit"]:
                end(state, "step_limit", f"本轮 {state['limit']} 步用尽，可用 --resume 续跑。")
        snapshot(state)
        store.commit(state)
    return report(state, "executed", count)
