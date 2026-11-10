"""python -m src.content_agent --help"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .domain import CATALOG, ROOT, HARD_LIMIT, TOOLS, parse_time, utc_now
from .catalog import validate_catalog
from .contracts import CONTRACTS, RESULT_SCHEMA
from .engine import run
from .store import Conflict, Store


def read_json(path):
    if str(path) == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_output(path, data):
    """Keep the previous complete export if writing or replacing fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".export-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description="预算与截止时间约束下的内容活动策划")
    p.add_argument("request", nargs="?", help="输入 JSON 路径")
    p.add_argument("--catalog", help="自定义合成素材、费用和耗时表 JSON")
    p.add_argument("--describe-tools", action="store_true", help="输出工具契约与模拟费用表")
    p.add_argument("--model", choices=["mock","api","bridge"], default="mock")
    p.add_argument("--db", default=".runtime/campaigns.sqlite")
    p.add_argument("--as-of", help="测试时钟，ISO 8601 且含时区；默认使用当前真实时间")
    p.add_argument("--max-steps", type=int, default=10, help=f"单轮步数，累计上限 {HARD_LIMIT}")
    p.add_argument("--fail-once", choices=TOOLS, help="活动中首次符合条件的指定调用失败")
    p.add_argument("--partial-once",action="store_true",help="首次多素材检索只成功一项，其余项可按 ID 重试")
    p.add_argument("--resume", action="store_true", help="重新开启已中断的活动")
    p.add_argument("--decision-file", help="bridge 单步响应 JSON 路径，可用 - 从 stdin 读取")
    p.add_argument("--external-name", default="external", help="记录外部控制器自报名称")
    p.add_argument("--out", help="将此次完整结果另存为 UTF-8 JSON")
    args = p.parse_args()
    store = None
    output_path = None
    try:
        if args.out:
            candidate = Path(args.out).resolve()
            protected = [args.request, args.db, args.db + "-wal", args.db + "-shm",
                         args.catalog or str(ROOT/"data/catalog.json")]
            if args.decision_file:
                protected.append(args.decision_file)
            if candidate in {Path(name).resolve() for name in protected if name and name != "-"}:
                raise ValueError("--out 不能覆盖输入文件、动作文件或检查点数据库")
            output_path = candidate
        if args.decision_file and args.model != "bridge":
            raise ValueError("--decision-file 仅用于 bridge")
        if args.catalog:
            catalog = read_json(args.catalog)
            validate_catalog(catalog)
            CATALOG.clear()
            CATALOG.update(catalog)
        else:
            validate_catalog(CATALOG)
        if args.describe_tools:
            output = {"status":"described","tools":CONTRACTS,"result_schema":RESULT_SCHEMA,"catalog":CATALOG}
            code = 0
        else:
            if not args.request:
                raise ValueError("需要输入 JSON 路径，或使用 --describe-tools")
            instant = parse_time(args.as_of).isoformat() if args.as_of else None
            store = Store(args.db)
            output = run(store, read_json(args.request), lambda: instant or utc_now().isoformat(),
                         args.model, args.max_steps, args.resume, args.fail_once,
                         read_json(args.decision_file) if args.decision_file else None, args.external_name,
                         partial_once=args.partial_once)
            code = 0 if output["status"] in {"completed","running"} else 2
    except (ValueError, OSError, sqlite3.Error, Conflict) as exc:
        output = {"status":"error", "reason":str(exc)}
        code = 3
    finally:
        if store:
            store.close()
    data = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if output_path is not None:
        try:
            write_output(output_path, data)
        except OSError as exc:
            print(f"无法导出 JSON：{exc}", file=sys.stderr)
            return 3
    sys.stdout.reconfigure(encoding="utf-8")
    print(data, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
