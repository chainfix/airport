import socket

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from airport.config import Store
from airport.certificates import Certificates
from airport.web import create_app


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def test_restart_uses_last_applied_not_saved_draft(tmp_path, certificate_files, config):
    root = tmp_path / 'data'
    store = Store(root)
    certs = Certificates(store)
    certs.commit(certs.stage(*certificate_files(), config['domain']))
    store.put('config', config)
    store.put('active', {'enabled': True, 'config': config})
    draft = {**config, 'android_host': '192.168.1.60'}
    store.put('config', draft)
    app = create_app(root, host='127.0.0.1', control_port=free_port(), pcm_port=free_port())
    async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)):
        service = app['service']
        assert service.gateway.servers
        assert service.gateway.config == config
        assert service.store.get('config') == draft
    assert not app['service'].gateway.servers


async def test_restart_with_invalid_certificate_fails_closed(tmp_path, config):
    store = Store(tmp_path / 'data')
    store.put('active', {'enabled': True, 'config': config})
    app = create_app(store.root, host='127.0.0.1', control_port=free_port(), pcm_port=free_port())
    async with TestClient(TestServer(app)):
        assert not app['service'].gateway.servers
        assert any('自动启动失败' in event['message'] for event in app['service'].gateway.events)
