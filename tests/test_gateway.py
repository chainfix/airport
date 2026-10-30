import asyncio
import socket
import ssl

import pytest

from airport.certificates import Certificates, inspect_certificate
from airport.config import Problem, Store, domain_name, validate_config
from airport.gateway import Gateway
from airport.diagnostics import check_control


@pytest.mark.parametrize("bad", ["https://phone.example.com", "phone.example.com:443", "phone.example.com/a", "127.0.0.1", "*.example.com", "example", "a..com", "-a.example.com"])
def test_bad_domains(bad):
    with pytest.raises(ValueError):
        domain_name(bad)


@pytest.mark.parametrize("field,value", [("android_host", "127.0.0.1"), ("android_host", "169.254.169.254"),
    ("android_host", "8.8.8.8"), ("android_host", "localhost"), ("public_control_port", "443; id"),
    ("public_pcm_port", 17595), ("android_pcm_port", 7592), ("public_pcm_port", 8787),
    ("android_control_port", True), ("android_control_port", 0), ("android_control_port", 65536)])
def test_validation_field_errors(config, field, value):
    config[field] = value
    with pytest.raises(Problem) as error:
        validate_config(config)
    assert field in error.value.fields


def test_config_normalization(config):
    config["domain"] = " PHONE.EXAMPLE.COM "
    config["public_pcm_port"] = "17596"
    value = validate_config(config)
    assert value["domain"] == "phone.example.com"
    assert value["public_pcm_port"] == 17596
    assert domain_name("电话.example.com").startswith("xn--")


def test_certificate_rejects_mismatch_expiry_and_key(certificate_files):
    cert, key = certificate_files()
    assert inspect_certificate(cert, key, "phone.example.test")["days_left"] >= 59
    for c, k, domain in [(cert, key, "wrong.example.test"), (*certificate_files(expired=True), "phone.example.test"),
                         (cert, certificate_files()[1], "phone.example.test"), (b"invalid", key, "phone.example.test")]:
        with pytest.raises(Problem):
            inspect_certificate(c, k, domain)


def test_untrusted_certificate_rejected(certificate_files, monkeypatch):
    cert, key = certificate_files()
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(Problem, match="信任链"):
        inspect_certificate(cert, key, "phone.example.test")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def echo(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_real_tls_both_channels_reload_and_plaintext_rejected(tmp_path, certificate_files, config):
    certs = Certificates(Store(tmp_path / "data"))
    cert, key = certificate_files()
    metadata = certs.stage(cert, key, config["domain"])
    certs.commit(metadata)
    backend = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = backend.sockets[0].getsockname()[1]
    config.update(android_host="127.0.0.1", android_control_port=port, android_pcm_port=port)
    gateway = Gateway("127.0.0.1", free_port(), free_port())
    client_context = ssl.create_default_context()
    connections = []
    try:
        await gateway.apply(config, certs.context(config["domain"]))
        for channel in ("control", "pcm"):
            reader, writer = await asyncio.open_connection("127.0.0.1", gateway.ports[channel], ssl=client_context, server_hostname=config["domain"])
            connections.append(writer)
            payload = (b"AIRSIMVWL1\x00\xffHMAC-header-preserved\n" * 4096)
            writer.write(payload)
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(payload)), 5) == payload
            # Swap TLS certificate while this stream remains connected.
            new_meta = certs.stage(*certificate_files(), config["domain"])
            await gateway.apply(config, certs.context(config["domain"], new_meta))
            writer.write(b"still-connected")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(15), 3) == b"still-connected"
            fresh_reader, fresh_writer = await asyncio.open_connection("127.0.0.1", gateway.ports[channel], ssl=client_context, server_hostname=config["domain"])
            connections.append(fresh_writer)
            fingerprint = __import__('hashlib').sha256(fresh_writer.get_extra_info('ssl_object').getpeercert(binary_form=True)).hexdigest()
            assert fingerprint == new_meta["fingerprint"]
        reader, writer = await asyncio.open_connection("127.0.0.1", gateway.ports["control"])
        writer.write(b"GET /v1/health HTTP/1.1\r\nHost: bad\r\n\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        for writer in connections:
            writer.close()
        await gateway.stop()
        backend.close()
        await backend.wait_closed()
    assert not gateway.tasks
    assert gateway.active == {"control": 0, "pcm": 0}


async def test_second_port_collision_rolls_back(tmp_path, certificate_files, config):
    certs = Certificates(Store(tmp_path / "data"))
    metadata = certs.stage(*certificate_files(), config["domain"])
    gateway = Gateway("127.0.0.1", free_port(), free_port())
    occupied = await asyncio.start_server(echo, "127.0.0.1", gateway.ports["pcm"])
    try:
        with pytest.raises(Problem, match="监听"):
            await gateway.apply(config, certs.context(config["domain"], metadata))
        assert not gateway.servers
        # The first port must be released even though only the second failed.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", gateway.ports["control"]))
    finally:
        occupied.close()
        await occupied.wait_closed()


async def test_diagnostic_distinguishes_basic_auth_from_device_auth():
    async def basic(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm="NAS"\r\nContent-Length: 0\r\n\r\n')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(basic, "127.0.0.1", 0)
    try:
        result = await check_control("127.0.0.1", server.sockets[0].getsockname()[1])
        assert result["status"] == "error"
        assert "网页" in result["message"]
    finally:
        server.close()
        await server.wait_closed()
