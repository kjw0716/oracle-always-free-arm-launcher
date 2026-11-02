"""Configuration, timing and control tests. No real Oracle API calls."""
import configparser
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

from test_core import g, settings, backend
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wizard
from tools import build_release
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "api.pem").write_bytes(self.private)
        (self.root / "ssh.pub").write_text("ssh-ed25519 dGVzdA== example", encoding="utf-8")
        self.config = self.root / "config.ini"
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read_string("""[oracle]
user=ocid1.user.oc1..example
tenancy=ocid1.tenancy.oc1..example
region=eu-frankfurt-1
fingerprint=placeholder
key_file=api.pem
[instance]
name=oci-a1
subnet_id=ocid1.subnet.oc1.example
ssh_public_key_file=ssh.pub
[retry]
interval_minutes=2.5
max_minutes=60
""")
        self.cfg["oracle"]["fingerprint"] = wizard.key_fingerprint(self.root / "api.pem")
        self.write()

    def write(self):
        with self.config.open("w", encoding="utf-8") as handle:
            self.cfg.write(handle)

    def test_minutes_and_region_are_portable(self):
        s = g.load_settings(self.config)
        self.assertEqual(s.interval, 150)
        self.assertEqual(s.max_minutes, 60)
        self.assertEqual(s.auth["region"], "eu-frankfurt-1")
        self.assertEqual(s.auth["key_file"], str(self.root / "api.pem"))

    def test_legacy_seconds_remain_supported(self):
        self.cfg["retry"].pop("interval_minutes")
        self.cfg["retry"]["interval_seconds"] = "120"
        self.write()
        self.assertEqual(g.load_settings(self.config).interval, 120)

    def test_timing_overrides_do_not_invalidate_idempotency(self):
        before = g.load_settings(self.config)
        after = g.load_settings(self.config, interval_minutes=30, max_minutes=90)
        self.assertEqual(after.interval, 1800)
        self.assertGreaterEqual(after.max_backoff, 1800)
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(g.load_settings(self.config).interval, 150)

    def test_invalid_time_values_fail_closed(self):
        for value in [0, .5, -1, 61, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(g.StopError):
                g.load_settings(self.config, interval_minutes=value)
        for value in [-1, .01, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(g.StopError):
                g.load_settings(self.config, max_minutes=value)

    def test_preview_only_imports_auth_and_discards_password(self):
        preview = "[DEFAULT]\nuser=u\ntenancy=t\nregion=r\nkey_file=<key> # TODO\npass_phrase=do-not-save\n"
        result = wizard.parse_preview(preview)
        self.assertEqual(result["key_file"], "<key>")
        self.assertNotIn("pass_phrase", result)
        with self.assertRaises(g.StopError):
            wizard.parse_preview("-----BEGIN PRIVATE KEY-----")

    def test_invalid_edit_preserves_existing_config(self):
        original = self.config.read_bytes()
        self.cfg["retry"]["interval_minutes"] = "0"
        with self.assertRaises(g.StopError):
            wizard.save_config(self.config, self.cfg)
        self.assertEqual(original, self.config.read_bytes())
        self.assertEqual(list(self.root.glob(".config-*.tmp")), [])

    def test_edit_cannot_invalidate_pending_request(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        g.write_json(runtime / "state.json", {"fingerprint": g.load_settings(self.config).fingerprint})
        original = self.config.read_bytes()
        self.cfg["instance"]["name"] = "changed"
        with self.assertRaises(g.StopError):
            wizard.save_config(self.config, self.cfg)
        self.assertEqual(original, self.config.read_bytes())

    def test_complete_manual_wizard_does_not_contact_cloud(self):
        answers = ["3", "", "", "", "", "", "", "2", "", "", "1", "3.5", "120"]
        with patch("builtins.input", side_effect=answers), patch.object(wizard, "discover") as cloud, contextlib.redirect_stdout(io.StringIO()):
            wizard.configure(self.config)
        cloud.assert_not_called()
        result = g.load_settings(self.config)
        self.assertEqual(result.interval, 210)
        self.assertEqual(result.max_minutes, 120)
        self.assertNotIn("PRIVATE KEY", self.config.read_text(encoding="utf-8"))

    def test_quick_timing_edit_never_reasks_credentials(self):
        with patch("builtins.input", side_effect=["5", "0"]), contextlib.redirect_stdout(io.StringIO()):
            wizard.edit_timing(self.config)
        result = g.load_settings(self.config)
        self.assertEqual(result.interval, 300)
        self.assertEqual(result.max_minutes, 0)

    def test_wizard_rejects_key_fingerprint_mismatch(self):
        self.cfg["oracle"]["fingerprint"] = "wrong"
        self.write()
        original = self.config.read_bytes()
        with patch("builtins.input", side_effect=["3", "", "", "", ""]), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(g.StopError):
            wizard.configure(self.config)
        self.assertEqual(original, self.config.read_bytes())


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.base = ["--config", str(self.root / "config.ini")]

    def test_stop_and_status_work_without_credentials(self):
        with patch.object(g, "Oracle") as cloud, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.main(self.base + ["--stop"]), 0)
            self.assertEqual(g.main(self.base + ["--status"]), 0)
        self.assertTrue((self.runtime / "STOP").exists())
        cloud.assert_not_called()

    def test_second_process_does_not_overwrite_status_or_error(self):
        g.write_json(self.runtime / "run.json", {"status": "waiting", "pid": 123})
        before = (self.runtime / "run.json").read_bytes()
        with g.single_process(self.runtime / "grab.lock"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.main(self.base), 2)
            self.assertTrue(g.is_running(self.runtime))
        self.assertFalse(g.is_running(self.runtime))
        self.assertEqual(before, (self.runtime / "run.json").read_bytes())
        self.assertFalse((self.runtime / "needs-attention.json").exists())

    def test_stop_marker_prevents_cloud_calls_until_explicit_resume(self):
        (self.runtime / "STOP").touch()
        with patch.object(g, "Oracle") as cloud:
            self.assertEqual(g.main(self.base), 0)
            cloud.assert_not_called()
        with patch.object(g, "load_settings", return_value=settings()), patch.object(g, "Oracle", return_value=backend()):
            self.assertEqual(g.main(self.base + ["--resume", "--once"]), 0)
        self.assertFalse((self.runtime / "STOP").exists())
        self.assertEqual(g.read_json(self.runtime / "run.json")["status"], "complete")

    def test_duration_clips_wait_and_stops_before_second_attempt(self):
        api = backend()
        api.launch.side_effect = TimeoutError()
        now = [0.0]
        with patch.object(g, "load_settings", return_value=settings(max_minutes=1)), \
             patch.object(g, "Oracle", return_value=api), \
             patch.object(g.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(g.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            self.assertEqual(g.main(self.base), 0)
        self.assertEqual(api.launch.call_count, 1)
        self.assertEqual(now[0], 60)
        self.assertEqual(g.read_json(self.runtime / "run.json")["status"], "time_limit")
        self.assertIn("pending", g.read_json(self.runtime / "state.json"))

    def test_stop_during_wait_is_responsive(self):
        api = backend()
        api.launch.side_effect = TimeoutError()
        with patch.object(g, "load_settings", return_value=settings()), \
             patch.object(g, "Oracle", return_value=api), \
             patch.object(g.time, "sleep", side_effect=lambda seconds: (self.runtime / "STOP").touch()):
            self.assertEqual(g.main(self.base), 0)
        self.assertEqual(api.launch.call_count, 1)
        self.assertEqual(g.read_json(self.runtime / "run.json")["status"], "stopped")


class ReleaseTests(unittest.TestCase):
    def test_packaging_excludes_personal_files_even_if_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "config.example.ini").write_text("[oracle]\nuser=\n[instance]\nssh_public_key_file=\n", encoding="utf-8")
            (root / "grab.py").write_text("print('example')", encoding="utf-8")
            (root / "config.ini").write_text("personal configuration", encoding="utf-8")
            (root / "private.pem").write_text("private key", encoding="utf-8")
            with patch.object(build_release, "ROOT", root), patch.object(build_release, "FILES", ("config.example.ini", "grab.py")), contextlib.redirect_stdout(io.StringIO()):
                archive = build_release.build()
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(set(z.namelist()), {"oracle-always-free-arm-launcher/config.example.ini", "oracle-always-free-arm-launcher/grab.py"})

    def test_packaging_refuses_long_realistic_identifier_in_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "config.example.ini").write_text("[oracle]\nuser=\n[instance]\nssh_public_key_file=\n", encoding="utf-8")
            (root / "grab.py").write_text("ocid1.user.oc1.." + "a" * 80, encoding="utf-8")
            with patch.object(build_release, "ROOT", root), patch.object(build_release, "FILES", ("config.example.ini", "grab.py")), self.assertRaises(ValueError):
                build_release.build()
            self.assertFalse((root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
