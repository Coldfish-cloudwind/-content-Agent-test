"""Run the submitted scenarios through the public CLI and aggregate evidence."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from examples.reproduce import AS_OF, CASES, ROOT
from src.content_agent.__main__ import read_json, write_output
from src.content_agent.evaluation import assess, matches_expectation, review_summary, review_template


def evaluate(mode, repeats, out):
    if not 1 <= repeats <= 10:
        raise ValueError("repeats 须在 1–10 之间")
    if mode == "api" and not os.environ.get("PLANNER_MODEL", "").strip():
        raise ValueError("api 评估须先设置 PLANNER_MODEL")
    out = out.resolve()
    # Keep evaluation exports away from source and authoritative submitted evidence.
    if out == ROOT or any(out == ROOT/name or ROOT/name in out.parents
                          for name in ("src","tests","data","docs")):
        raise ValueError("评估输出请选择独立目录，例如 .runtime/evaluation")
    out.mkdir(parents=True, exist_ok=True)
    (ROOT/".runtime").mkdir(exist_ok=True)
    expected = read_json(ROOT/"data/evaluation-expected.json")
    rows = []
    model = os.environ.get("PLANNER_MODEL") if mode == "api" else "deterministic-mock"
    with tempfile.TemporaryDirectory(dir=ROOT/".runtime") as tmp:
        for repeat in range(1, repeats+1):
            db = Path(tmp)/f"run-{repeat}.sqlite"
            for name, target, flags, exit_code in CASES:
                args = [sys.executable,"-m","src.content_agent",f"examples/inputs/{name}.json",
                        "--model",mode,"--db",str(db),"--as-of",AS_OF,*flags]
                begin = time.monotonic()
                try:
                    proc = subprocess.run(args,cwd=ROOT,capture_output=True,encoding="utf-8",timeout=360)
                    elapsed = time.monotonic()-begin
                    result = json.loads(proc.stdout)
                    request = read_json(ROOT/f"examples/inputs/{name}.json")
                    assessment = assess(request, result) if result.get("status") != "error" else {
                        "checks":{}, "all_checks_passed":False}
                    exp = expected[target]
                    matched = proc.returncode == exit_code and matches_expectation(result, exp)
                    record = {"case":target,"repeat":repeat,"model_mode":mode,"model":model,
                              "elapsed_seconds":round(elapsed,4),"exit_code":proc.returncode,
                              "expected":exp,"expectation_matched":matched,"assessment":assessment,
                              "result":result}
                except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                    record = {"case":target,"repeat":repeat,"model_mode":mode,"model":model,
                              "expectation_matched":False,"assessment":{"all_checks_passed":False},
                              "harness_error":type(exc).__name__}
                write_output(out/f"{target}-{repeat}.json",json.dumps(record,ensure_ascii=False,indent=2)+"\n")
                rows.append(record)
    completed = [r for r in rows if r.get("result",{}).get("status") == "completed"]
    checked = [r for r in rows if r["assessment"]["all_checks_passed"]]
    matched = [r for r in rows if r["expectation_matched"]]
    summary = {"mode":mode,"model":model,"repeats":repeats,"cases":len(rows),
               "expectation_matches":len(matched),"expectation_match_rate":len(matched)/len(rows),
               "structural_checks_passed":len(checked),"completion_count":len(completed),
               "completion_rate":len(completed)/len(rows),
               "mean_elapsed_seconds":sum(r.get("elapsed_seconds",360) for r in rows)/len(rows),
               "human_review_status":"pending","api_token_usage":"not_collected",
               "note":"完成率包含预期未完成的负例；预期匹配率评估验收行为。mock 重复运行只验证流程稳定性。"}
    write_output(out/"summary.json",json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    review_path = out/"human-review.json"
    if not review_path.exists():
        write_output(review_path,json.dumps([review_template(r["case"]+"-"+str(r["repeat"]))
                                            for r in completed],ensure_ascii=False,indent=2)+"\n")
    return summary


def main():
    p = argparse.ArgumentParser(description="批量运行与结构评分，真实模型使用 --model api")
    p.add_argument("--model",choices=["mock","api"],default="mock")
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--out",type=Path,default=ROOT/".runtime/evaluation")
    p.add_argument("--review-file",type=Path,help="只验证并汇总已填写的人工评分文件")
    args = p.parse_args()
    try:
        if args.review_file:
            reviews = read_json(args.review_file)
            if not isinstance(reviews,list) or not all(isinstance(r,dict) for r in reviews):
                raise ValueError("人工评分文件须为数组")
            output = [{"case_id":r.get("case_id"),**review_summary(r)} for r in reviews]
        else:
            output = evaluate(args.model,args.repeats,args.out)
        print(json.dumps(output,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError) as exc:
        print(json.dumps({"status":"error","reason":str(exc)},ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
