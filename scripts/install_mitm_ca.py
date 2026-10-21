"""Bootstrap the project's MITM CA in the Windows current-user root store."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = r"Cert:\CurrentUser\Root"


def prepare_ca(confdir: Path, ca_path: Path, *, check_only: bool) -> str | None:
    """Reuse the signing CA; never rotate it or trust an unrelated public cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    key_path = confdir / "mitmproxy-ca.pem"
    if not key_path.is_file():
        # from_store would replace an orphaned public CA. Require manual recovery
        # instead, including when the public certificate lives outside confdir.
        if ca_path.exists() or any(confdir.glob("mitmproxy-ca*")):
            raise RuntimeError("CA private-key file is missing; restore the original CA backup before retrying.")
        if check_only:
            return None
        from mitmproxy.certs import CertStore

        CertStore.from_store(confdir, "mitmproxy", 2048)

    signing_pem = key_path.read_bytes()
    signing_cert = x509.load_pem_x509_certificate(signing_pem)
    key = serialization.load_pem_private_key(signing_pem, password=None)
    if key.public_key() != signing_cert.public_key():
        raise RuntimeError("The signing CA certificate and private key do not match.")
    if not signing_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise RuntimeError("The signing certificate is not a CA.")
    now = datetime.now(timezone.utc)
    if not signing_cert.not_valid_before_utc <= now <= signing_cert.not_valid_after_utc:
        raise RuntimeError("The signing CA is expired or not yet valid; recover it before retrying.")

    thumbprint = signing_cert.fingerprint(hashes.SHA1()).hex().upper()
    if ca_path.is_file():
        public_bytes = ca_path.read_bytes()
        if b"PRIVATE KEY" in public_bytes:
            raise RuntimeError("The configured public CA file contains a private key.")
        public_cert = (
            x509.load_pem_x509_certificate(public_bytes)
            if b"-----BEGIN CERTIFICATE-----" in public_bytes
            else x509.load_der_x509_certificate(public_bytes)
        )
        if public_cert.fingerprint(hashes.SHA1()).hex().upper() != thumbprint:
            raise RuntimeError("The configured public CA does not match this project's signing CA.")
    elif check_only:
        return None
    else:
        # Export only the public certificate, including for custom ca_cert_path.
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_bytes(signing_cert.public_bytes(serialization.Encoding.PEM))
    return thumbprint


def is_trusted(thumbprint: str) -> bool:
    # Use the same store and exact fingerprint as the GUI's CA diagnostics.
    if len(thumbprint) != 40 or any(c not in "0123456789ABCDEF" for c in thumbprint):
        raise ValueError("Invalid CA thumbprint")
    script = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "$store=[System.Security.Cryptography.X509Certificates.X509Store]::new('Root','CurrentUser'); "
        "try { $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly); "
        f"$matches=@($store.Certificates | Where-Object {{$_.Thumbprint -eq '{thumbprint}'}}); "
        "ConvertTo-Json -Compress -InputObject ([bool]($matches.Count -gt 0)) "
        "} finally { $store.Close() }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Could not read the Windows current-user root certificate store.")
    value = json.loads(result.stdout.strip())
    if not isinstance(value, bool):
        raise RuntimeError("Unexpected certificate-store response.")
    return value


def app_request(endpoint: str, *, post: bool = False) -> dict:
    # Never send this local control request through a configured network proxy.
    request = urllib.request.Request(
        "http://127.0.0.1:8766/api/ca/" + endpoint,
        data=b"{}" if post else None,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def install_through_app(ca_path: Path, thumbprint: str) -> None:
    status = app_request("status")
    certificate = status.get("projectCertificate") or {}
    app_thumbprint = "".join(str(certificate.get("thumbprint", "")).split()).upper()
    if (not status.get("ok")
            or Path(status.get("currentCaPath") or "").resolve() != ca_path.resolve()
            or app_thumbprint != thumbprint):
        raise RuntimeError("The running Access app is not using this project's CA path and fingerprint.")
    result = app_request("install", post=True)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("message") or "The running app could not install the CA."))


def install_ca(ca_path: Path, thumbprint: str, *, check_only: bool,
               via_running_app: bool = False) -> str:
    if is_trusted(thumbprint):
        return "already-installed"
    if check_only:
        return "not-installed"
    if via_running_app:
        install_through_app(ca_path, thumbprint)
    else:
        result = subprocess.run(
            ["certutil.exe", "-user", "-addstore", "Root", str(ca_path)],
            capture_output=True, timeout=60,
        )
        if result.returncode:
            raise RuntimeError(
                f"certutil failed (exit 0x{result.returncode & 0xffffffff:08X}); "
                "run in the Access desktop user's terminal, or use --via-running-app from SSH."
            )
    if not is_trusted(thumbprint):
        raise RuntimeError("Installation completed, but the project's CA fingerprint was not found in CurrentUser\\Root.")
    return "installed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check only; do not create files or install a certificate")
    parser.add_argument("--via-running-app", action="store_true",
                        help="install through the Access desktop session's local API on port 8766 (for SSH)")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "data/custom.yaml",
                        help="custom YAML, resolved relative to the project root")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "status": "error", "message": "This installer supports Windows only."}))
        return 2
    try:
        # Resolve from this script rather than the shell's working directory.
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.config.config_loader import load_app_config

        config_path = args.config if args.config.is_absolute() else PROJECT_ROOT / args.config
        config = load_app_config(config_path, project_root=PROJECT_ROOT)
        thumbprint = prepare_ca(config.proxy.confdir, config.proxy.ca_cert_path, check_only=args.check)
        status = (install_ca(config.proxy.ca_cert_path, thumbprint, check_only=args.check,
                             via_running_app=args.via_running_app)
                  if thumbprint else "missing")
        installed = status in {"installed", "already-installed"}
        print(json.dumps({
            "ok": installed, "status": status, "thumbprint": thumbprint,
            "caPath": str(config.proxy.ca_cert_path), "storePath": STORE_PATH,
        }))
        return 0 if installed else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "error", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
