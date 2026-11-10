"""Validation, scheduling arithmetic and the durable campaign representation."""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / "data/catalog.json").read_text(encoding="utf-8"))
TOOLS = ("search_assets", "estimate_cost", "generate_script", "save_plan")
HARD_LIMIT = 32


def parse_time(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("需要带时区的 ISO 8601 时间")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("时间超出可表示的 UTC 范围") from exc


def utc_now():
    return datetime.now(timezone.utc)


def after(instant, minutes):
    try:
        return (parse_time(instant) + timedelta(minutes=minutes)).isoformat()
    except OverflowError as exc:
        raise ValueError("制作结束时间超出可表示范围") from exc


def validate(request):
    if not isinstance(request, dict):
        raise ValueError("输入必须是 JSON 对象")
    key = request.get("campaign_id")
    if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key):
        raise ValueError("campaign_id 必须为 1–64 位字母、数字、下划线或连字符")
    issues = []
    if not isinstance(request.get("topic"), str) or not request["topic"].strip():
        issues.append("缺少主题 topic")
    if type(request.get("budget")) is not int or request["budget"] < 0:
        issues.append("budget 必须为非负整数模拟单位")
    try:
        parse_time(request.get("deadline"))
    except (ValueError, OverflowError):
        issues.append("deadline 需要带时区的 ISO 8601 截止时间")
    return issues


def signature(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def initial(request, start):
    return dict(request=request, start=start, catalog_version=CATALOG["version"],
                catalog_signature=signature(CATALOG),
                assets=[], quotes={}, script=None, saved_plan=None, ledger=[],
                history=[], status="running", reason=None, session=1, used=0,
                revision=0, injected=[], steps={name: "pending" for name in TOOLS})


def spent(state):
    return sum(row["units"] for row in state["ledger"])


def cursor_time(state, now):
    last = state["ledger"][-1]["finish"] if state["ledger"] else state["start"]
    return max(parse_time(last), parse_time(now)).isoformat()


def supports(state, variant):
    kinds = {asset["kind"] for asset in state["assets"]}
    needs = CATALOG["variants"][variant]["requires"]
    return bool(kinds) and (needs == ["any"] or set(needs) <= kinds)


def calc_quote(state, variant, now):
    spec = CATALOG["variants"][variant]
    save = CATALOG["common"]["save_plan"]
    remaining = save["units"] + (0 if state["script"] else spec["units"])
    minutes = save["minutes"] + (0 if state["script"] else spec["minutes"])
    total = spent(state) + remaining
    finish = after(cursor_time(state, now), minutes)
    delta = (parse_time(finish) - parse_time(state["request"]["deadline"])).total_seconds()
    gap = max(0, total - state["request"]["budget"])
    delay = max(0, math.ceil(delta / 60))
    return dict(variant=variant, total=total, remaining_units=remaining, finish=finish,
                budget_gap=gap, late_minutes=delay, feasible=not (gap or delay))
