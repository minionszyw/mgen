import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mgen import cli, common, files, http


class CommonTests(unittest.TestCase):
    def test_entrypoint_and_help(self):
        for args in [
            ["--version"],
            ["--help"],
            ["video", "run", "--help"],
            ["image", "run", "--help"],
        ]:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main(args)
            self.assertEqual(cm.exception.code, 0)
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-m", "mgen", "image", "validate", "--prompt", "猫"],
                cwd=directory,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('"valid": true', completed.stdout)

    def test_atomic_no_overwrite_and_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result"
            files.atomic_write(path, b"original")
            with self.assertRaises(FileExistsError):
                files.atomic_write(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".mgen-*")), [])
            files.atomic_write(path, b"replacement", True)
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_retry_after_invalid_values(self):
        for value in ["nan", "inf", "-inf", "not-a-date"]:
            r = Mock(headers={"Retry-After": value})
            self.assertEqual(http.retry_delay(r, 2), 4)
        self.assertEqual(http.retry_delay(Mock(headers={"Retry-After": "-1"}), 2), 0)

    def test_malformed_api_error_is_still_retryable(self):
        self.assertTrue(http.JsonClient.retryable(None, 500, {"error": {"code": [], "type": {}}}))

    def test_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text('{"value": NaN}')
            with self.assertRaises(common.CliError):
                common.read_json(path)


if __name__ == "__main__":
    unittest.main()
