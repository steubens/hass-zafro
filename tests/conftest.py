"""Shared fixtures for the device-level integration tests.

The sys.path shim that used to live here is gone -- ``pyproject.toml``'s
``[tool.pytest.ini_options]`` now points pytest at the add-on package
directly. What remains is genuinely shared test infrastructure: a throwaway
self-signed TLS certificate (for ``DeviceServer``'s listener) and a free TCP
port to bind it to.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest


def _generate_cert_with_cryptography(cert_path: Path, key_path: Path, common_name: str) -> None:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _generate_cert_with_openssl(cert_path: Path, key_path: Path, common_name: str) -> None:
    openssl_binary = shutil.which("openssl")
    if openssl_binary is None:
        pytest.skip("neither the 'cryptography' package nor an openssl binary is available to generate a test TLS certificate")
    subprocess.run(
        [
            openssl_binary,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tls_cert_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway self-signed cert/key pair for ``DeviceServer``'s TLS listener.

    Prefers the ``cryptography`` package (fast, no subprocess); falls back to
    the ``openssl`` CLI; skips the test entirely if neither is available
    rather than failing (this is infrastructure, not something under test).
    """
    cert_path = tmp_path / "device-server-cert.pem"
    key_path = tmp_path / "device-server-key.pem"
    try:
        _generate_cert_with_cryptography(cert_path, key_path, "zafro-bridge-test")
    except ImportError:
        _generate_cert_with_openssl(cert_path, key_path, "zafro-bridge-test")
    return cert_path, key_path


@pytest.fixture
def free_tcp_port() -> int:
    """An ephemeral TCP port that is free at the moment of the call.

    ``aiohttp.web.TCPSite`` does not expose the "bind port 0, ask the OS"
    pattern directly, so tests that need a real listening port pick one this
    way instead. Unavoidably racy in theory (another process could grab it
    between this call and the server's own bind), but in practice reliable
    enough for a short-lived test process.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
