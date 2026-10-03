import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import FritzBot  # noqa: E402


class FritzBotPasswordFileTests(unittest.TestCase):
    def test_password_file_expands_one_job_per_password_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            password_file = temp_path / "passwords.txt"
            password_file.write_text("wrong-pass\n\nright-pass\n", encoding="utf-8")

            jobs_file = temp_path / "jobs.json"
            jobs_file.write_text(
                json.dumps([
                    {"address": "10.0.0.2", "service": "DeviceInfo", "action": "GetInfo"},
                ]),
                encoding="utf-8",
            )

            args = argparse.Namespace(
                jobs_file=str(jobs_file), password_stdin=False, password=None,
                password_file=str(password_file), address="10.0.0.1", port=None,
                user="dslf-config", no_user=False, password_env="FRITZ_PASSWORD",
                tls=False, service="DeviceInfo", action="GetInfo", parameter=[],
                workers=2, log_file=str(temp_path / "output.jsonl"),
            )

            output = io.StringIO()
            progress = io.StringIO()
            with patch.object(FritzBot, "_default_jobs_file", return_value=str(jobs_file)), \
                    patch.object(FritzBot, "_run_job", side_effect=[
                        {"job": 1, "status": "skipped", "reason": "authentication failed"},
                        {"job": 2, "status": "success", "address": "10.0.0.2", "data": {"ok": True}},
                    ]) as run_job, redirect_stdout(output), redirect_stderr(progress):
                exit_code = FritzBot.main([
                    "--password-file", str(password_file), "--workers", "2",
                    "--log-file", str(temp_path / "output.jsonl"),
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(run_job.call_count, 2)
            calls_by_line = sorted(run_job.call_args_list, key=lambda call: call.args[1]["password_line"])
            self.assertEqual([call.args[1]["password"] for call in calls_by_line], ["wrong-pass", "right-pass"])
            self.assertEqual([call.args[1]["password_line"] for call in calls_by_line], [1, 3])
            self.assertNotIn("authentication failed", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())[0]["password_line"], 3)
            self.assertIn("Progress: 2/2", progress.getvalue())


if __name__ == "__main__":
    unittest.main()
