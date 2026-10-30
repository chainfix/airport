import json

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from airport.web import create_app


@pytest.fixture
async def client(tmp_path):
    app = create_app(tmp_path / "data", host="127.0.0.1", control_port=0, pcm_port=0)
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
        yield client


async def post(client, path, data=None, origin=True):
    headers = {"Origin": str(client.make_url('/')).rstrip('/')} if origin else {}
    return await client.post(path, json=data or {}, headers=headers)


async def setup(client):
    code = client.app["service"].setup_code
    response = await post(client, "/api/auth/setup", {"code": code, "password": "correct horse battery"})
    assert response.status == 200, await response.text()


async def test_auth_bootstrap_csrf_and_session(client):
    assert (await client.get('/api/state')).status == 401
    response = await client.get('/api/auth/status')
    assert (await response.json())["setup_required"]
    assert (await post(client, '/api/auth/setup', {"code": "bad", "password": "correct horse battery"})).status == 403
    assert (await post(client, '/api/auth/setup', {"code": "错误", "password": "correct horse battery"})).status == 403
    assert (await post(client, '/api/auth/login', {}, origin=False)).status == 403
    await setup(client)
    assert not (client.app["service"].store.root / 'setup-code').exists()
    assert (await client.get('/api/state')).status == 200
    assert (await post(client, '/api/auth/setup', {})).status == 409
    assert (await client.get('/api/state', headers={'Host': 'evil.example'})).status == 403
    assert (await post(client, '/api/auth/logout')).status == 200
    assert (await client.get('/api/state')).status == 401
    assert (await post(client, '/api/auth/login', {'password': 'correct horse battery'})).status == 200


async def test_bad_config_does_not_replace_saved_config(client, config):
    await setup(client)
    assert (await post(client, '/api/config', config)).status == 200
    wrong = {**config, 'domain': 'https://bad.example.com', 'public_pcm_port': 17595}
    response = await post(client, '/api/config', wrong)
    assert response.status == 400
    fields = (await response.json())['fields']
    assert 'domain' in fields and 'public_pcm_port' in fields
    response = await client.get('/api/state')
    assert (await response.json())['config'] == config
    assert not client.app['service'].gateway.servers


async def test_credentials_not_returned_and_export_safe(client, config):
    await setup(client)
    await post(client, '/api/config', config)
    payload = {'provider': 'cloudflare', 'email': 'user@example.com', 'agree': True,
               'credentials': {'CF_DNS_API_TOKEN': 'test-token-not-for-real-service'}}
    assert (await post(client, '/api/acme/settings', payload)).status == 200
    for path in ('/api/state', '/api/export'):
        text = await (await client.get(path)).text()
        assert 'test-token-not-for-real-service' not in text
        assert 'correct horse battery' not in text
    credentials_file = client.app['service'].store.root / 'acme.json'
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    payload['credentials'] = {'CF_DNS_API_TOKEN': ''}
    assert (await post(client, '/api/acme/settings', payload)).status == 200
    assert json.loads(credentials_file.read_text())['credentials']['CF_DNS_API_TOKEN'] == 'test-token-not-for-real-service'


async def test_wrong_certificate_preserves_current(client, config, certificate_files):
    await setup(client)
    await post(client, '/api/config', config)
    cert, key = certificate_files()
    response = await post(client, '/api/certificate/upload', {'certificate': cert.decode(), 'private_key': key.decode()})
    assert response.status == 200, await response.text()
    before = client.app['service'].certificates.metadata()
    cert, key = certificate_files('other.example.test')
    response = await post(client, '/api/certificate/upload', {'certificate': cert.decode(), 'private_key': key.decode()})
    assert response.status == 400
    assert client.app['service'].certificates.metadata()['id'] == before['id']


async def test_no_certificate_cannot_start(client, config):
    await setup(client)
    await post(client, '/api/config', config)
    response = await post(client, '/api/gateway/start')
    assert response.status == 400
    assert not client.app['service'].gateway.servers


async def test_login_rate_limit(client):
    for _ in range(10):
        assert (await post(client, '/api/auth/login', {'password': 'incorrect'})).status == 401
    assert (await post(client, '/api/auth/login', {'password': 'incorrect'})).status == 429


async def test_password_change_invalidates_old_password(client):
    await setup(client)
    response = await post(client, '/api/auth/password', {'old_password': 'correct horse battery', 'password': 'a new very long password'})
    assert response.status == 200
    await post(client, '/api/auth/logout')
    assert (await post(client, '/api/auth/login', {'password': 'correct horse battery'})).status == 401
    assert (await post(client, '/api/auth/login', {'password': 'a new very long password'})).status == 200


async def test_static_security_headers(client):
    response = await client.get('/')
    assert response.status == 200
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert (await client.get('/config.json')).status == 404
