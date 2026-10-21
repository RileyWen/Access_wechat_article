import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "install_mitm_ca", Path(__file__).resolve().parents[1] / "scripts/install_mitm_ca.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PrepareCaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.confdir = self.root / "mitm"
        self.public = self.confdir / "mitmproxy-ca-cert.cer"

    def test_check_missing_does_not_create_files(self):
        self.assertIsNone(installer.prepare_ca(self.confdir, self.public, check_only=True))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bootstrap_reuses_ca_and_repairs_only_public_export(self):
        first = installer.prepare_ca(self.confdir, self.public, check_only=False)
        original = {p.name: p.read_bytes() for p in self.confdir.iterdir()}
        self.assertEqual(first, installer.prepare_ca(self.confdir, self.public, check_only=False))
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.confdir.iterdir()})
        custom = self.root / "custom" / "ca.cer"
        self.assertIsNone(installer.prepare_ca(self.confdir, custom, check_only=True))
        self.assertFalse(custom.exists())
        self.assertEqual(first, installer.prepare_ca(self.confdir, custom, check_only=False))
        self.assertNotIn(b"PRIVATE KEY", custom.read_bytes())

    def test_orphaned_public_ca_is_not_rotated(self):
        installer.prepare_ca(self.confdir, self.public, check_only=False)
        (self.confdir / "mitmproxy-ca.pem").unlink()
        before = {p.name: p.read_bytes() for p in self.confdir.iterdir()}
        with self.assertRaisesRegex(RuntimeError, "private-key file is missing"):
            installer.prepare_ca(self.confdir, self.public, check_only=False)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.confdir.iterdir()})

    def test_unrelated_public_ca_is_rejected(self):
        installer.prepare_ca(self.confdir, self.public, check_only=False)
        other = self.root / "other"
        other_ca = other / "mitmproxy-ca-cert.cer"
        installer.prepare_ca(other, other_ca, check_only=False)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            installer.prepare_ca(self.confdir, other_ca, check_only=False)


class InstallCaTests(unittest.TestCase):
    def test_already_installed_and_check_only_never_import(self):
        for trusted, check_only, expected in [(True, False, "already-installed"),
                                               (False, True, "not-installed")]:
            with self.subTest(trusted=trusted, check_only=check_only):
                with patch.object(installer, "is_trusted", return_value=trusted), \
                     patch.object(installer.subprocess, "run") as run:
                    self.assertEqual(installer.install_ca(Path("ca.cer"), "A" * 40,
                                                         check_only=check_only), expected)
                    run.assert_not_called()

    def test_successful_import_must_be_verified(self):
        with patch.object(installer, "is_trusted", side_effect=[False, False]), \
             patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(RuntimeError, "fingerprint was not found"):
                installer.install_ca(Path("ca.cer"), "A" * 40, check_only=False)

    def test_remote_install_rejects_another_apps_ca_before_posting(self):
        for path, thumbprint in [("other.cer", "A" * 40), ("ca.cer", "B" * 40)]:
            with self.subTest(path=path, thumbprint=thumbprint):
                status = {"ok": True, "currentCaPath": path,
                          "projectCertificate": {"thumbprint": thumbprint}}
                with patch.object(installer, "app_request", return_value=status) as request:
                    with self.assertRaisesRegex(RuntimeError, "path and fingerprint"):
                        installer.install_through_app(Path("ca.cer"), "A" * 40)
                    request.assert_called_once_with("status")

    def test_remote_install_verifies_the_user_store_after_app_import(self):
        status = {"ok": True, "currentCaPath": "ca.cer",
                  "projectCertificate": {"thumbprint": "A" * 40}}
        with patch.object(installer, "app_request", side_effect=[status, {"ok": True}]) as request, \
             patch.object(installer, "is_trusted", side_effect=[False, True]), \
             patch.object(installer.subprocess, "run") as run:
            self.assertEqual(installer.install_ca(Path("ca.cer"), "A" * 40, check_only=False,
                                                 via_running_app=True), "installed")
            self.assertEqual(request.call_count, 2)
            request.assert_called_with("install", post=True)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
