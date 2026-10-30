from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
def certificate_files(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "airport test CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=100)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

    def issue(domain="phone.example.test", expired=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
                .not_valid_after(now + timedelta(days=-1 if expired else 60))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(ca_key, hashes.SHA256()))
        return (cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM),
                key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return issue


@pytest.fixture
def config():
    return {"domain": "phone.example.test", "android_host": "192.168.1.50", "android_control_port": 7592,
            "android_pcm_port": 7593, "public_control_port": 17595, "public_pcm_port": 17596}
