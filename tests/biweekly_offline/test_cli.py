import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from fixtures import source


class CliTests(unittest.TestCase):
    def test_dry_run_is_default_then_explicit_offline_run_is_idempotent(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            input_path = base / "synthetic.json"
            input_path.write_text(json.dumps(source()))
            output = base / "output"
            command = [
                sys.executable,
                str(root / "scripts/biweekly_offline_demo.py"),
                "--input",
                str(input_path),
                "--output",
                str(output),
            ]
            env = {
                "PYTHONPATH": str(root / "packages/agric_satellite_analysis_common")
                + ":"
                + str(root / "services/ingest"),
                "PYTHONUTF8": "1",
            }
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, check=True
            )
            self.assertEqual(json.loads(result.stdout)["status"], "disabled")
            self.assertFalse(output.exists())
            first = json.loads(
                subprocess.run(
                    command + ["--offline"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            second = json.loads(
                subprocess.run(
                    command + ["--offline"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(second["attempts"], 1)
            self.assertEqual(second["status"], "ready")
            self.assertTrue(
                Path(second["download_file"]).read_bytes().startswith(b"%PDF-")
            )
