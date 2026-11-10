"""Local business tools. The engine commits tool effects and the trace together."""
from .domain import CATALOG, TOOLS, after, calc_quote, cursor_time, parse_time, spent, supports
from .contracts import envelope


def result(ok, code, **details):
    return {"ok": ok, "code": code, **details}


def charge(state, name, price, now):
    start = cursor_time(state, now)
    state["ledger"].append(dict(tool=name, units=price["units"], minutes=price["minutes"],
                                start=start, finish=after(start, price["minutes"])))


def search(state, now, asset_ids=None, partial_once=False):
    pending = state.setdefault("asset_failures",[])
    if asset_ids is not None:
        if not isinstance(asset_ids,list) or not asset_ids or any(not isinstance(key,str) for key in asset_ids):
            return result(False,"not_pending_assets",message="素材 ID 必须为非空字符串数组。")
        retrieved = {a["id"] for a in state["assets"]}
        if all(key in retrieved and key not in pending for key in asset_ids):
            return result(True,"reused_assets",assets=state["assets"],retrieved_asset_ids=[],failed_asset_ids=pending)
        if any(key not in pending for key in asset_ids):
            return result(False,"not_pending_assets",message="只能按 ID 重试已失败的素材。")
    if state["assets"] and not pending:
        return result(True, "reused_assets", assets=state["assets"])
    if state["assets"] and asset_ids is None:
        return result(True,"assets_partial",assets=state["assets"],failed_asset_ids=pending,
                      retrieved_asset_ids=[],message="部分素材待重试，请提供失败素材 ID。")
    topic = state["request"]["topic"].casefold()
    hits = [a for a in CATALOG["assets"] if any(tag.casefold() in topic for tag in a["tags"])]
    if asset_ids is not None:
        hits = [a for a in hits if a["id"] in asset_ids]
    if not hits:
        return result(False, "no_assets", topic=topic, message="合成库无匹配素材，可补充素材或调整主题。")
    price = {"units":0,"minutes":0} if state["assets"] else CATALOG["common"]["search_assets"]
    gap = max(0, spent(state) + price["units"] - state["request"]["budget"])
    if gap:
        return result(False, "search_over_budget", budget_gap=gap)
    finish = after(cursor_time(state, now), price["minutes"])
    if parse_time(finish) > parse_time(state["request"]["deadline"]):
        return result(False, "search_too_late", earliest_search_finish=finish)
    if price["units"] or price["minutes"]:
        charge(state, "search_assets", price, now)
    code = "assets_found"
    if partial_once and "partial_search" not in state["injected"] and len(hits) > 1:
        state["injected"].append("partial_search")
        state["asset_failures"] = [a["id"] for a in hits[1:]]
        hits = hits[:1]
        code = "assets_partial"
    existing = {a["id"]:a for a in state["assets"]}
    existing.update({a["id"]:a for a in hits})
    state["assets"] = list(existing.values())
    retrieved = [a["id"] for a in hits]
    state["asset_failures"] = [key for key in state["asset_failures"] if key not in retrieved]
    items = [{"asset_id":a["id"],"status":"success","cost":price["units"] if i == 0 else 0,"error":None}
             for i,a in enumerate(hits)]
    items.extend({"asset_id":key,"status":"failed","cost":0,
                  "error":{"code":"synthetic_fetch_failure","message":"模拟该素材获取失败，可按 ID 重试。"}}
                 for key in state["asset_failures"])
    return result(True, code, assets=state["assets"],retrieved_asset_ids=retrieved,
                  failed_asset_ids=state["asset_failures"],item_results=items, available_variants=[
        v for v in CATALOG["variants"] if supports(state, v)])


def make_script(state, variant):
    topic = state["request"]["topic"]
    assets = state["assets"]
    title = f"{topic}｜一起参与"
    if variant == "studio":
        clip = next(a for a in assets if a["kind"] == "clip")
        photo = next(a for a in assets if a["kind"] == "image")
        beats = [
            {"position": "0–5 秒", "asset": clip["id"], "visual": clip["detail"], "copy": f"第一次参加{topic}？跟镜头一起看看。"},
            {"position": "5–22 秒", "asset": photo["id"], "visual": photo["detail"], "copy": f"收藏这张「{photo['title']}」，到现场时对照查看。"},
            {"position": "22–30 秒", "asset": photo["id"], "visual": "定格并叠加评论提示", "copy": "把最想体验的环节写在评论区，约一位朋友一起参加。"}]
    else:
        asset = next((a for a in assets if a["kind"] == "image"), assets[0])
        beats = [{"position":"首图（视频素材取首帧）", "asset":asset["id"], "visual":asset["detail"], "copy":title},
                 {"position":"正文", "asset":asset["id"], "visual":"裁切主视觉并叠加互动提示",
                  "copy":f"看到「{asset['title']}」，你想先体验什么？收藏活动卡，留言约个搭子。"}]
    return dict(title=title, variant=variant, channel=CATALOG["variants"][variant]["channel"],
                objective="引导潜在参与者收藏活动信息、留言并邀请同伴。", beats=beats)


def _execute(state, action, args, now, fail_once=None, partial_once=False):
    if action not in TOOLS:
        return result(False, "unknown_tool")
    needs_variant = action in ("estimate_cost", "generate_script")
    expected_args = {"variant"} if needs_variant else set()
    if action == "search_assets":
        expected_args = {"asset_ids"} if "asset_ids" in args else set()
    if set(args) != expected_args:
        return result(False, "invalid_arguments")
    variant = args.get("variant")
    if needs_variant and (not isinstance(variant, str) or variant not in CATALOG["variants"]):
        return result(False, "unknown_variant")
    if action != "search_assets" and not state["assets"]:
        return result(False, "search_required")
    if needs_variant and not supports(state, variant):
        return result(False, "insufficient_assets", variant=variant)
    if state["script"] and needs_variant and state["script"]["variant"] != variant:
        return result(False, "script_already_committed", variant=state["script"]["variant"])
    if action == "generate_script" and state["script"]:
        return result(True, "reused_script", variant=variant)
    if action == "generate_script" and variant not in state["quotes"]:
        return result(False, "estimate_required", variant=variant)
    if action == "save_plan" and not state["script"]:
        return result(False, "script_required")
    if action == "save_plan" and state["script"]["variant"] not in state["quotes"]:
        return result(False, "estimate_required")
    if action == "save_plan" and state["saved_plan"]:
        return result(True,"reused_plan",id=state["request"]["campaign_id"],total_cost=spent(state))
    if action == "search_assets" and state["assets"] and not state.get("asset_failures"):
        return search(state,now,args.get("asset_ids"))
    if action in ("generate_script", "save_plan"):
        variant = variant or state["script"]["variant"]
        quote = calc_quote(state, variant, now)
        if not quote["feasible"]:
            return result(False, "constraints_exceeded", **quote)
    if action == fail_once and action not in state["injected"]:
        state["injected"].append(action)
        return result(False, "injected_unavailable", message="本次调用未执行，费用为零。")
    if action == "search_assets":
        return search(state, now,args.get("asset_ids"),partial_once)
    if action == "estimate_cost":
        quote = calc_quote(state, variant, now)
        state["quotes"][variant] = quote
        return result(True, "quote", **quote)
    if action == "generate_script":
        state["script"] = make_script(state, variant)
        charge(state, action, CATALOG["variants"][variant], now)
        return result(True, "script_ready", variant=variant, title=state["script"]["title"],
                      segments=len(state["script"]["beats"]))
    charge(state, action, CATALOG["common"][action], now)
    owners = {"search_assets":"素材运营", "generate_script":"内容编辑", "save_plan":"活动运营"}
    state["saved_plan"] = dict(campaign_id=state["request"]["campaign_id"],
        topic=state["request"]["topic"], deadline=state["request"]["deadline"],
        budget=state["request"]["budget"], total_cost=spent(state), script=state["script"],
        production_schedule=[{**row, "owner":owners[row["tool"]]} for row in state["ledger"]],
        handoff=["按分镜排版或剪辑", "发布前核对活动信息与字幕", "发布后记录收藏和评论数量"])
    return result(True, "saved", variant=variant, total_cost=spent(state),
                  id=state["request"]["campaign_id"], finish=state["ledger"][-1]["finish"])


def execute(state, action, args, now, fail_once=None, partial_once=False):
    before_cost = spent(state)
    before_minutes = sum(c["minutes"] for c in state["ledger"])
    raw = _execute(state,action,args,now,fail_once,partial_once)
    return envelope(state,action,args,raw,spent(state)-before_cost,
                    sum(c["minutes"] for c in state["ledger"])-before_minutes)
