"""Launch the built workbench with explicit production or isolated demo storage."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ROOT / ".workdeps"


def main():
    parser = argparse.ArgumentParser(description="凭证工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--demo", action="store_true", help="本地快照，独立数据库，不回写飞书")
    args = parser.parse_args()
    if not (ROOT / "frontend/dist/index.html").is_file():
        parser.error("请先在 frontend 执行 npm ci 和 npm run build")
    if args.demo:
        os.environ["WORKBENCH_OFFLINE"] = "1"
        os.environ["WORKBENCH_DB"] = str(ROOT / "tmp/workbench-demo.sqlite3")
        os.environ["WORKBENCH_OUTPUT"] = str(ROOT / "tmp/workbench-demo-exports")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if DEPENDENCIES.is_dir():
        sys.path.insert(0, str(DEPENDENCIES))
        os.environ["PYTHONPATH"] = str(DEPENDENCIES) + os.pathsep + os.environ.get("PYTHONPATH", "")
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
