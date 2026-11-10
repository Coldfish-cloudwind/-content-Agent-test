"""Regenerate offline examples via the public CLI in a temporary database."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2030-05-01T09:00:00+08:00"
CASES = [
    ("normal", "normal", [], 0), ("normal", "repeat", [], 0),
    ("replan", "replan", [], 0),
    ("failure", "failure", ["--fail-once", "generate_script"], 0),
    ("missing_budget", "missing_budget", [], 2), ("missing_deadline","missing_deadline",[],2),
    ("no_budget", "no_budget", [], 2), ("short_time", "short_time", [], 0),
    ("no_assets", "no_assets", [], 2), ("partial_assets", "partial_assets", [], 0),
    ("resume", "interrupted", ["--fail-once","save_plan","--max-steps","4"], 2),
    ("resume", "resumed", ["--resume","--fail-once","save_plan"], 0),
    ("retry_variant", "retry_interrupted", ["--fail-once","generate_script"], 2),
    ("retry_variant", "retry_completed", ["--resume","--fail-once","generate_script"], 0),
    ("partial_retry", "partial_retry", ["--partial-once"], 0),
    ("partial_resume", "partial_retained", ["--partial-once","--max-steps","1"], 2),
    ("partial_resume", "partial_resumed", ["--resume","--partial-once"], 0),
    ("dependency_blocked", "dependency_blocked", ["--fail-once","search_assets","--max-steps","1"], 2),
]


def reproduce(out):
    out.mkdir(parents=True, exist_ok=True)
    (ROOT / '.runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / '.runtime') as tmp:
        for name, target, flags, code in CASES:
            args = [sys.executable, "-m", "src.content_agent", f"examples/inputs/{name}.json",
                    "--db", str(Path(tmp)/"db.sqlite"), "--as-of", AS_OF, *flags]
            proc = subprocess.run(args, cwd=ROOT, capture_output=True, encoding="utf-8")
            if proc.returncode != code:
                raise RuntimeError(f"{target}: {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
            result = json.loads(proc.stdout)
            (out / f"{target}.json").write_text(proc.stdout, encoding="utf-8")
            print(f"{target:18} {result['status']:12} {result['total_cost']:2}  {result['invocation']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT/".runtime"/"examples")
    reproduce(parser.parse_args().out)
