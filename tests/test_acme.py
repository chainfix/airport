import asyncio
import sys

import pytest

from airport.acme import ACME
from airport.config import Problem, Store


def configure(acme, domain="phone.example.test"):
    acme.configure({'provider': 'cloudflare', 'email': 'user@example.com', 'agree': True,
                    'credentials': {'CF_DNS_API_TOKEN': 'test-secret'}}, domain)


async def test_acme_builds_argument_array_and_installs(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    installed = []
    async def install(*args):
        installed.append(args)
    acme = ACME(store, install)
    configure(acme)
    captured = {}

    class Process:
        returncode = 0
        async def wait(self):
            folder = store.root / 'acme/cloudflare/certificates'
            folder.mkdir(parents=True)
            (folder / 'phone.example.test.crt').write_bytes(b'cert')
            (folder / 'phone.example.test.key').write_bytes(b'key')
            return 0

    async def spawn(*args, **kwargs):
        captured.update(args=args, env=kwargs['env'])
        return Process()
    monkeypatch.setattr('airport.acme.shutil.which', lambda _: '/lego')
    monkeypatch.setattr('airport.acme.asyncio.create_subprocess_exec', spawn)
    acme.start('phone.example.test')
    await acme.task
    assert acme.job['state'] == 'success'
    assert captured['args'][-1] == 'run'
    assert 'test-secret' not in captured['args']
    assert captured['env']['CF_DNS_API_TOKEN'] == 'test-secret'
    assert installed == [(b'cert', b'key', 'phone.example.test', 'acme')]
    assert 'test-secret' not in str(acme.job)
    assert 'test-secret' not in str(acme.settings())
    with pytest.raises(Problem, match='一分钟'):
        acme.start('phone.example.test')


async def test_acme_failure_does_not_install(tmp_path, monkeypatch):
    async def install(*args):
        pytest.fail('must not install failed certificate')
    acme = ACME(Store(tmp_path / 'data'), install)
    configure(acme)
    class Process:
        returncode = 1
        async def wait(self): return 1
    async def spawn(*args, **kwargs): return Process()
    monkeypatch.setattr('airport.acme.shutil.which', lambda _: '/lego')
    monkeypatch.setattr('airport.acme.asyncio.create_subprocess_exec', spawn)
    acme.start('phone.example.test')
    await acme.task
    assert acme.job['state'] == 'error'
    assert 'test-secret' not in str(acme.job)


async def test_acme_renew_uses_existing_certificate(tmp_path, monkeypatch):
    async def install(*args): pass
    acme = ACME(Store(tmp_path / 'data'), install)
    configure(acme)
    folder = acme.store.root / 'acme/cloudflare/certificates'
    folder.mkdir(parents=True)
    (folder / 'phone.example.test.crt').write_bytes(b'cert')
    (folder / 'phone.example.test.key').write_bytes(b'key')
    captured = []
    class Process:
        returncode = 0
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        captured.extend(args)
        return Process()
    monkeypatch.setattr('airport.acme.shutil.which', lambda _: '/lego')
    monkeypatch.setattr('airport.acme.asyncio.create_subprocess_exec', spawn)
    acme.start('phone.example.test')
    await acme.task
    assert captured[-3:] == ['renew', '--days', '30']
    with pytest.raises(Problem, match='当前域名'):
        acme.start('changed.example.test')
