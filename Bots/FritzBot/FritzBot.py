"""Concurrent FRITZ!Box data collector using fritzconnection.

Use a JSON job file to query multiple devices and actions. Each job may contain:
address, port, user, password_env, tls, service, action, and arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ADDRESS = "10.0.0.1"
DEFAULT_PORT = 49000
DEFAULT_USER = "dslf-config"


def _authentication_failure(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ("401", "unauthorized", "authentication", "invalid password", "login failed"))


def _parse_parameter(value: str) -> tuple[str, Any]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("parameters must use KEY=JSON_VALUE")
    name, raw_value = value.split("=", 1)
    try:
        return name, json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"invalid JSON parameter {name}: {error}") from error


def _read_passwords_from_file(path: str | os.PathLike[str]) -> list[tuple[int, str]]:
    with Path(path).open(encoding="utf-8") as source:
        passwords = [
            (line_number, line.strip())
            for line_number, line in enumerate(source, 1)
            if line.strip()
        ]
    return passwords


def _load_jobs(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.jobs_file:
        with Path(args.jobs_file).open(encoding="utf-8") as source:
            jobs = json.load(source)
        if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
            raise ValueError("jobs file must contain a JSON array of objects")

        if args.password_stdin and args.password is not None:
            for job in jobs:
                if "password" not in job and "password_env" not in job:
                    job["password"] = args.password
        elif args.password is not None:
            for job in jobs:
                if "password" not in job and "password_env" not in job:
                    job["password"] = args.password
        return jobs

    return [{
        "address": args.address,
        "port": args.port,
        "user": "" if args.no_user else args.user,
        "password": args.password,
        "password_env": None if args.password_file else args.password_env,
        "tls": args.tls,
        "service": args.service,
        "action": args.action,
        "arguments": dict(args.parameter or []),
    }]


def _expand_password_attempts(jobs: list[dict[str, Any]], password_file: str) -> list[dict[str, Any]]:
    passwords = _read_passwords_from_file(password_file)
    if not passwords:
        raise ValueError(f"password file {password_file} contains no passwords")

    attempts: list[dict[str, Any]] = []
    for job in jobs:
        if "password" in job or job.get("password_env"):
            attempts.append(job)
            continue
        for line_number, password in passwords:
            attempts.append({**job, "password": password, "password_line": line_number})
    return attempts


def _run_job(job_number: int, job: dict[str, Any], log_path: Path, log_lock: threading.Lock) -> dict[str, Any]:
    try:
        from fritzconnection import FritzConnection
    except ImportError as error:
        raise RuntimeError("install fritzconnection with: python -m pip install fritzconnection") from error

    password = job.get("password")
    if password is None and job.get("password_env"):
        password = os.environ.get(str(job["password_env"]))
    address = str(job.get("address", DEFAULT_ADDRESS))
    if password is None:
        return {"job": job_number, "status": "skipped", "reason": "no password supplied", "address": address}

    service = str(job["service"])
    action = str(job["action"])
    try:
        connection = FritzConnection(
            address=address,
            port=int(job.get("port") or (49443 if job.get("tls") else DEFAULT_PORT)),
            user=str(job.get("user", DEFAULT_USER)),
            password=password,
            use_tls=bool(job.get("tls", False)),
        )
        result = connection.call_action(service, action, **dict(job.get("arguments", {})))
    except Exception as error:
        if _authentication_failure(error):
            return {"job": job_number, "status": "skipped", "reason": "authentication failed", "address": address}
        return {"job": job_number, "status": "failed", "error": str(error), "address": address}

    record = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "job": job_number,
        "address": address,
        "service": service,
        "action": action,
        "arguments": dict(job.get("arguments", {})),
        "data": result,
    }
    with log_lock:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    success = {"job": job_number, "status": "success", "address": address, "data": result}
    if "password_line" in job:
        success["password_line"] = job["password_line"]
    return success


def _default_jobs_file() -> str | None:
    default_path = Path(__file__).with_name("jobs.json")
    return str(default_path) if default_path.exists() else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect FRITZ!Box action data concurrently")
    parser.add_argument("--jobs-file", default=_default_jobs_file(), help="JSON array of independent jobs; defaults to jobs.json next to this script")
    parser.add_argument("--address", default=os.getenv("FRITZ_ADDRESS", DEFAULT_ADDRESS))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=os.getenv("FRITZ_USER", DEFAULT_USER))
    parser.add_argument("--no-user", action="store_true")
    parser.add_argument("--password")
    parser.add_argument("--password-file", help="Read passwords from a file, one password per line")
    parser.add_argument("--password-stdin", action="store_true", help="Read the password from standard input")
    parser.add_argument("--password-env", default="FRITZ_PASSWORD")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--service", default="DeviceInfo")
    parser.add_argument("--action", default="GetInfo")
    parser.add_argument("--parameter", action="append", type=_parse_parameter, metavar="KEY=JSON_VALUE")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--log-file", default="fritz-data.jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.workers < 1:
        print("--workers must be at least 1", file=sys.stderr)
        return 2
    if args.password_stdin:
        args.password = sys.stdin.readline().rstrip("\r\n")
        if not args.password:
            print("No password was provided on standard input", file=sys.stderr)
            return 2
    try:
        jobs = _load_jobs(args)
        if args.password_file:
            jobs = _expand_password_attempts(jobs, args.password_file)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Could not load jobs: {error}", file=sys.stderr)
        return 2
    if not jobs:
        print("No jobs supplied", file=sys.stderr)
        return 2

    log_path = Path(args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()
    successful_results: list[dict[str, Any]] = []
    progress_lock = threading.Lock()
    active_workers = 0
    completed = 0
    total = len(jobs)

    def run_with_progress(number: int, job: dict[str, Any]) -> dict[str, Any]:
        nonlocal active_workers, completed
        with progress_lock:
            active_workers += 1
            print(
                f"Workers running: {active_workers}/{min(args.workers, total)} | "
                f"Progress: {completed}/{total} password attempts",
                file=sys.stderr,
                flush=True,
            )
        try:
            result = _run_job(number, job, log_path, log_lock)
            if result.get("status") == "success" and "password_line" in job:
                result["password_line"] = job["password_line"]
            return result
        except Exception as error:
            return {"job": number, "status": "failed", "error": str(error)}
        finally:
            with progress_lock:
                active_workers -= 1
                completed += 1
                print(
                    f"Workers running: {active_workers}/{min(args.workers, total)} | "
                    f"Progress: {completed}/{total} password attempts",
                    file=sys.stderr,
                    flush=True,
                )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(run_with_progress, number, job)
            for number, job in enumerate(jobs, 1)
        ]
        for future in as_completed(futures):
            result = future.result()
            if result.get("status") == "success":
                successful_results.append(result)

    successful_results.sort(key=lambda result: result["job"])
    print(json.dumps(successful_results, indent=2, sort_keys=True, default=str))
    return 0 if successful_results else 1


if __name__ == "__main__":
    raise SystemExit(main())
