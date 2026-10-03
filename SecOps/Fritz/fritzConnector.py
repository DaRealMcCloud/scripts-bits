"""Collect useful details from a FRITZ!Box using fritzconnection.

Install the dependency with::

	python -m pip install fritzconnection

Examples::

	python fritzConnector.py --password "router-password"
	python fritzConnector.py --address 192.168.178.1 --user admin --json
	'router-password' | python fritzConnector.py
	FRITZ_PASSWORD="router-password" python fritzConnector.py
	python fritzConnector.py --input-file prefixes.txt --no-user --json
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_ADDRESS = "10.0.0.1"
DEFAULT_PORT = 49000
DEFAULT_TLS_PORT = 49443
DEFAULT_USER = "dslf-config"


def _get_action(connection: Any, service: str, action: str) -> dict[str, Any] | None:
	"""Call an optional service without failing the whole report."""
	try:
		return connection.call_action(service, action)
	except Exception as error:  # Service support differs between FRITZ!Box models.
		return {"error": str(error)}


def collect_details(connection: Any) -> dict[str, Any]:
	"""Return basic device details using a single FRITZ!Box action."""
	return {"device": _get_action(connection, "DeviceInfo", "GetInfo")}


def _is_authentication_error(details: dict[str, Any]) -> bool:
	"""Identify the authentication failure returned by the FRITZ!Box."""
	error = details.get("device", {}).get("error", "")
	error_text = str(error).lower()
	return "401 unauthorized" in error_text or "errorcode: 401" in error_text


def _generated_passwords(input_file: str, suffix_digits: int, start_line: int = 0, start_number: int = 0):
	"""Yield candidates and their source position."""
	with Path(input_file).open(encoding="utf-8") as source:
		for line_number, line in enumerate(source):
			if line_number < start_line:
				continue
			prefix = line.rstrip("\r\n")
			number_start = start_number if line_number == start_line else 0
			for number in range(number_start, 10**suffix_digits):
				yield line_number, number, f"{prefix}{number:0{suffix_digits}d}"


def _load_checkpoint(path: Path) -> dict[str, Any] | None:
	"""Load a saved candidate position, if one exists."""
	try:
		with path.open(encoding="utf-8") as checkpoint:
			return json.load(checkpoint)
	except (OSError, json.JSONDecodeError):
		return None


def _save_checkpoint(path: Path, input_file: str, line_number: int, number: int) -> None:
	"""Persist the next candidate position before trying it."""
	with path.open("w", encoding="utf-8") as checkpoint:
		json.dump(
			{"input_file": str(Path(input_file).resolve()), "line": line_number, "number": number},
			checkpoint,
			indent=2,
		)


def _ask_to_resume(checkpoint: dict[str, Any], input_file: str) -> bool:
	"""Ask whether a previous candidate position should be restored."""
	if checkpoint.get("input_file") != str(Path(input_file).resolve()):
		return False
	answer = input(
		f"Found saved progress at line {checkpoint.get('line', 0) + 1}, "
		f"number {checkpoint.get('number', 0):04d}. Resume? [Y/n] "
	)
	return answer.strip().lower() not in {"n", "no"}


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Report details from a FRITZ!Box")
	parser.add_argument(
		"-i",
		"--address",
		default=os.getenv("FRITZ_ADDRESS", DEFAULT_ADDRESS),
		help=f"FRITZ!Box IP/hostname (default: {DEFAULT_ADDRESS})",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=os.getenv("FRITZ_PORT"),
		help=f"HTTP port (default: {DEFAULT_PORT})",
	)
	parser.add_argument(
		"-u",
		"--user",
		default=os.getenv("FRITZ_USER", DEFAULT_USER),
		help="FRITZ!Box user (default: dslf-config)",
	)
	parser.add_argument(
		"--no-user",
		action="store_true",
		help="Do not send a username (for boxes configured for password-only login)",
	)
	parser.add_argument(
		"-p",
		"--password",
		default=os.getenv("FRITZ_PASSWORD"),
		help="FRITZ!Box password; FRITZ_PASSWORD is also supported",
	)
	parser.add_argument(
		"--password-stdin",
		action="store_true",
		help="Read the password from standard input",
	)
	parser.add_argument(
		"--input-file",
		metavar="FILE",
		help="Generate password candidates from each line in FILE",
	)
	parser.add_argument(
		"--suffix-digits",
		type=int,
		default=4,
		help="Number of zero-padded digits appended to each input line (default: 4)",
	)
	parser.add_argument(
		"--memory-file",
		metavar="FILE",
		help="Progress file; defaults to INPUT_FILE.progress.json",
	)
	parser.add_argument(
		"--retries",
		type=int,
		default=2,
		help="Additional attempts for each candidate after a failed request (default: 2)",
	)
	parser.add_argument(
		"--retry-delay",
		type=float,
		default=0.5,
		help="Seconds to wait between retries (default: 0.5)",
	)
	parser.add_argument(
		"--tls",
		action="store_true",
		help="Use HTTPS/TLS instead of HTTP",
	)
	parser.add_argument(
		"--json",
		action="store_true",
		help="Print the report as JSON",
	)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = _build_parser().parse_args(argv)
	if args.suffix_digits < 1:
		print("--suffix-digits must be at least 1", file=sys.stderr)
		return 2
	if args.retries < 0 or args.retry_delay < 0:
		print("--retries and --retry-delay cannot be negative", file=sys.stderr)
		return 2
	if args.input_file and not Path(args.input_file).is_file():
		print(f"Input file not found: {args.input_file}", file=sys.stderr)
		return 2
	if args.input_file and args.password is not None:
		print("Use either --input-file or --password, not both", file=sys.stderr)
		return 2

	checkpoint_path = None
	start_line = 0
	start_number = 0
	if args.input_file:
		checkpoint_path = Path(args.memory_file) if args.memory_file else Path(f"{args.input_file}.progress.json")
		checkpoint = _load_checkpoint(checkpoint_path) if checkpoint_path.exists() else None
		if checkpoint and _ask_to_resume(checkpoint, args.input_file):
			start_line = int(checkpoint.get("line", 0))
			start_number = int(checkpoint.get("number", 0))

	password_from_stdin = args.password_stdin or (
		args.password is None and not args.input_file and not sys.stdin.isatty()
	)

	if args.input_file:
		passwords = _generated_passwords(args.input_file, args.suffix_digits, start_line, start_number)
	elif args.password is None and password_from_stdin:
		args.password = sys.stdin.readline().rstrip("\r\n")
		passwords = iter((args.password,))
	elif args.password is None:
		args.password = getpass.getpass("FRITZ!Box password: ")
		passwords = iter((args.password,))
	else:
		passwords = iter((args.password,))

	if args.port is None:
		args.port = DEFAULT_TLS_PORT if args.tls else DEFAULT_PORT

	try:
		from fritzconnection import FritzConnection
	except ImportError:
		print(
			"Missing dependency: install it with 'python -m pip install fritzconnection'.",
			file=sys.stderr,
		)
		return 2

	if args.input_file:
		with Path(args.input_file).open(encoding="utf-8") as source:
			total_words = sum(1 for _ in source)
	else:
		total_words = None
	total_candidates = total_words * 10**args.suffix_digits if total_words is not None else None
	started_at = time.perf_counter()
	completed_candidates = 0
	for candidate_position in passwords:
		if args.input_file:
			line_number, number, password = candidate_position
			_save_checkpoint(checkpoint_path, args.input_file, line_number, number)
		else:
			password = candidate_position
		if not password:
			continue
		args.password = password
		for attempt in range(args.retries + 1):
			try:
				connection = FritzConnection(
					address=args.address,
					port=args.port,
					user="" if args.no_user else args.user,
					password=args.password,
					use_tls=args.tls,
				)
				details = collect_details(connection)
			except Exception as error:
				print(f"Could not connect to {args.address}: {error}", file=sys.stderr)
				return 1
			if not _is_authentication_error(details):
				break
			if attempt < args.retries:
				time.sleep(args.retry_delay)

		completed_candidates += 1
		if args.input_file:
			elapsed = time.perf_counter() - started_at
			average = elapsed / completed_candidates
			completed_total = line_number * 10**args.suffix_digits + number + 1
			remaining = total_candidates - completed_total
			eta = datetime.now() + timedelta(seconds=average * remaining)
			print(
				f"Word {line_number + 1}/{total_words}, number {number + 1}/{10**args.suffix_digits}; "
				f"attempt {attempt + 1}/{args.retries + 1}; "
				f"average {average:.2f}s; ETA {eta:%Y-%m-%d %H:%M:%S}",
				file=sys.stderr,
			)

		if not _is_authentication_error(details):
			if checkpoint_path and checkpoint_path.exists():
				checkpoint_path.unlink()
			break
	else:
		print("No password candidate succeeded", file=sys.stderr)
		return 1

	print(json.dumps(details, indent=2, sort_keys=True, default=str))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
