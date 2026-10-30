import hashlib
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from .config import Problem, atomic_write


def hostname_matches(pattern, hostname):
    pattern = pattern.lower()
    return pattern == hostname or (
        pattern.startswith("*.") and hostname.count(".") == pattern.count(".")
        and hostname.endswith(pattern[1:])
    )


def inspect_certificate(cert_pem, key_pem, domain, verify_trust=True):
    try:
        chain = x509.load_pem_x509_certificates(cert_pem)
        if not chain:
            raise ValueError()
        cert = chain[0]
        key = serialization.load_pem_private_key(key_pem, password=None)
        encode = lambda key: key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if encode(key.public_key()) != encode(cert.public_key()):
            raise Problem("证书和私钥不匹配，请上传同一套 fullchain.pem 与 privkey.pem。")
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        if not any(hostname_matches(name, domain) for name in names):
            raise Problem("证书不包含当前域名，请检查域名拼写或重新申请证书。")
        now = datetime.now(timezone.utc)
        if cert.not_valid_before_utc > now:
            raise Problem("证书尚未生效，请检查 NAS 系统时间。")
        if cert.not_valid_after_utc <= now:
            raise Problem("证书已过期，请续期后重新导入。")
        with tempfile.TemporaryDirectory(prefix="airport-cert-") as folder:
            folder = Path(folder)
            atomic_write(folder / "fullchain.pem", cert_pem)
            atomic_write(folder / "key.pem", key_pem)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(folder / "fullchain.pem", folder / "key.pem")
            if verify_trust:
                atomic_write(folder / "leaf.pem", cert.public_bytes(serialization.Encoding.PEM))
                args = ["openssl", "verify", "-purpose", "sslserver", "-verify_hostname", domain]
                if len(chain) > 1:
                    atomic_write(folder / "chain.pem", b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain[1:]))
                    args += ["-untrusted", str(folder / "chain.pem")]
                args += [str(folder / "leaf.pem")]
                result = subprocess.run(args, capture_output=True, timeout=10)
                if result.returncode:
                    raise Problem("证书信任链验证失败。请上传完整 fullchain.pem；不要使用自签名证书。")
        return {
            "domain": domain, "names": names,
            "expires_at": cert.not_valid_after_utc.isoformat(),
            "days_left": (cert.not_valid_after_utc - now).days,
            "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        }
    except Problem:
        raise
    except (ValueError, TypeError, x509.ExtensionNotFound, ssl.SSLError):
        raise Problem("无法读取证书或私钥。请上传 PEM 格式完整证书链和未加密私钥。") from None
    except (OSError, subprocess.TimeoutExpired):
        raise Problem("证书校验工具运行失败，请检查容器是否完整安装。") from None


class Certificates:
    def __init__(self, store):
        self.store = store

    def metadata(self):
        data = self.store.get("certificate")
        if data:
            data["days_left"] = (datetime.fromisoformat(data["expires_at"]) - datetime.now(timezone.utc)).days
        return data

    def stage(self, cert_pem, key_pem, domain, source="upload"):
        metadata = inspect_certificate(cert_pem, key_pem, domain)
        identity = hashlib.sha256(cert_pem + key_pem).hexdigest()
        folder = self.store.root / "certificates" / identity
        atomic_write(folder / "fullchain.pem", cert_pem)
        atomic_write(folder / "privkey.pem", key_pem)
        metadata.update(id=identity, source=source)
        return metadata

    def context(self, domain, metadata=None):
        metadata = metadata or self.metadata()
        if not metadata:
            raise Problem("还没有证书，请先申请或导入域名证书。")
        folder = self.store.root / "certificates" / metadata["id"]
        inspect_certificate((folder / "fullchain.pem").read_bytes(), (folder / "privkey.pem").read_bytes(), domain)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(folder / "fullchain.pem", folder / "privkey.pem")
        return context

    def commit(self, metadata):
        self.store.put("certificate", metadata)
        # Keep the previous certificate for inspection/rollback; no private keys leave this directory.
