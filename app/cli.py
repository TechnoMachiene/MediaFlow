"""Manual control. Needs .env with GEMINI_API_KEY and GEMINI_MODEL (real Gemini calls).

python -m app.cli submit uploads/cat.jpg --user 1
python -m app.cli worker --drain
python -m app.cli status <job_id>
python -m app.cli stats
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import threading

from dotenv import load_dotenv

from app.api.database import create_database
from app.api.persistence import persist_api_result
from app.config import Settings
from app.errors import ConfigError, MediaError
from app.wiring import build_runtime


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("path")
    s.add_argument("--user", default="0")
    w = sub.add_parser("worker")
    w.add_argument("--drain", action="store_true", help="exit when queue empty")
    st = sub.add_parser("status")
    st.add_argument("job_id")
    sub.add_parser("stats")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        rt = build_runtime(Settings.from_env())
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "submit":
        try:
            print(json.dumps(rt.store.submit(args.user, args.path)))
        except MediaError as e:
            print(f"rejected: {e}", file=sys.stderr)
            return 1
    elif args.cmd == "worker":
        if shutil.which("ffmpeg") is None:
            logging.warning(
                "ffmpeg is not installed; videos larger than MAX_VIDEO_MB=%d will fail compression",
                rt.settings.max_video_mb,
            )
        database_url = os.environ.get("DATABASE_URL", "").strip()
        db_engine = None
        if database_url:
            db_engine, session_factory = create_database(database_url)
            rt.worker.on_result = lambda job_id, result: persist_api_result(
                session_factory, job_id, result
            )
        try:
            if args.drain:
                print(json.dumps(rt.worker.drain()))
            else:
                stop = threading.Event()
                rt.worker.run(stop)
        except KeyboardInterrupt:
            if not args.drain:
                stop.set()
        finally:
            if db_engine is not None:
                db_engine.dispose()
    elif args.cmd == "status":
        print(json.dumps(rt.store.status(args.job_id), indent=2))
    elif args.cmd == "stats":
        print(json.dumps({**rt.queue.depths(), "gemini_calls_today": rt.limiter.used_today()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
