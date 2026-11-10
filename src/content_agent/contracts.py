"""Published tool contracts and a common, machine-readable result envelope."""
from .domain import CATALOG, signature

CONTRACTS = {
    "search_assets": {
        "inputs":{"asset_ids":"可选，非空字符串数组，仅重试上次失败的素材 ID；省略时按主题检索"},
        "outputs":["assets","available_variants","retrieved_asset_ids","failed_asset_ids"],
        "prerequisites":[], "partial_success":True,"idempotent":True,
        "idempotency":"同一任务的检索包只收费一次，已获得素材按 ID 去重；仅重试失败 ID",
    },
    "estimate_cost": {
        "inputs":{"variant":"studio | lite"}, "outputs":["total","remaining_units","finish","budget_gap","late_minutes","feasible"],
        "prerequisites":["search_assets"], "partial_success":False,"idempotent":True,
        "idempotency":"无收费或外部副作用，按当前时钟和账本重新估算",
    },
    "generate_script": {
        "inputs":{"variant":"studio | lite"}, "outputs":["variant","title","segments"],
        "prerequisites":["search_assets","estimate_cost"], "partial_success":False,"idempotent":True,
        "idempotency":"同一任务成功脚本复用，失败可重试，已付费脚本不切换版本",
    },
    "save_plan": {
        "inputs":{}, "outputs":["id","total_cost","finish"],
        "prerequisites":["estimate_cost","generate_script"], "partial_success":False,"idempotent":True,
        "idempotency":"同一任务已保存方案直接复用，不重复收费",
    },
}

RESULT_SCHEMA = {
    "step_id":"计划步骤 ID，独立调用时为工具名",
    "status":["success","partial_success","failed","blocked"],
    "cost":"本次实际新增的非负整数模拟单位",
    "duration":"本次实际新增的模拟分钟数",
    "output":"结构化业务输出对象",
    "error":"成功为 null，失败或部分成功为含 code 和 message 的对象",
    "idempotency_key":"任务、工具、参数及素材费率摘要的 SHA-256",
    "reused":"是否复用已有结果；复用本身不收费",
    "ok":"兼容字段，完整成功或有可用部分产物时为 true；用 status 判断完整性",
}


def tool_key(state, action, args):
    return signature({"task":state["request"]["campaign_id"],"tool":action,"args":args,
                      "catalog":state.get("catalog_signature", signature(CATALOG))})


def price(action, variant=None):
    if action == "generate_script":
        spec = CATALOG["variants"].get(variant) if isinstance(variant,str) else None
        return {"units":spec["units"],"minutes":spec["minutes"]} if spec else {"units":None,"minutes":None}
    return CATALOG["common"].get(action, {"units":0,"minutes":0})


def envelope(state, action, args, raw, cost=0, duration=0):
    partial = raw.get("code") == "assets_partial"
    status = "partial_success" if partial else ("success" if raw["ok"] else "failed")
    if raw.get("code") in ("search_required","estimate_required","script_required","insufficient_assets"):
        status = "blocked"
    error = None
    if status != "success":
        error = {"code":raw["code"],"message":raw.get("message",raw["code"])}
    variant = args.get("variant") or (state.get("script") or {}).get("variant")
    estimate = price(action,variant)
    return {**raw, "step_id":action,"status":status, "cost":cost, "duration":duration,
            "estimated_cost":estimate["units"], "estimated_duration":estimate["minutes"],
            "output":{k:v for k,v in raw.items() if k not in ("ok","code","message")},
            "error":error, "reused":raw.get("code","").startswith("reused_"),
            "idempotency_key":tool_key(state,action,args)}
