import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import save_key_local
import start_live


class LocalKeyTests(unittest.TestCase):
    def test_save_and_start_without_printing_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "deepseek.key"
            output = io.StringIO()
            with patch.object(save_key_local, "KEY_PATH", path), patch("save_key_local.getpass.getpass", return_value="test-only-secret"), patch("sys.stdout", output):
                save_key_local.main()
            self.assertEqual(path.read_text(encoding="utf-8"), "test-only-secret")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertNotIn("test-only-secret", output.getvalue())
            startup_output = io.StringIO()
            with patch.dict(os.environ, {"DEEPSEEK_KEY_FILE": str(path), "PORT": "5052"}, clear=True), patch.object(start_live.app, "run") as run, patch("sys.stdout", startup_output):
                start_live.main()
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "test-only-secret")
                run.assert_called_once_with(host="127.0.0.1", port=5052, debug=False)
            self.assertNotIn("test-only-secret", startup_output.getvalue())

    def test_start_rejects_key_file_with_broad_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deepseek.key"
            path.write_text("test-only-secret", encoding="utf-8")
            path.chmod(0o644)
            with patch.dict(os.environ, {"DEEPSEEK_KEY_FILE": str(path)}, clear=True), patch.object(start_live.app, "run"):
                with self.assertRaisesRegex(SystemExit, "权限过宽"):
                    start_live.main()


if __name__ == "__main__":
    unittest.main()
