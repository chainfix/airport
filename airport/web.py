import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .acme import ACME, PROVIDERS
from .certificates import Certificates
from .config import Problem, Store, private_address, validate_config
from .diagnostics import diagnose
from .gateway import Gateway

STATIC = Path(__file__).parent / "static"


class Service:
    def __init__(self, data_dir, host="0.0.0.0", control_port=17595, pcm_port=17596, panel_port=8787):
        self.store = Store(data_dir)
        self.panel_port = panel_port
        self.certificates = Certificates(self.store)
        self.gateway = Gateway(host, control_port, pcm_port)
        self.acme = ACME(self.store, self.install_certificate)
        self.sessions = {}
        self.attempts = OrderedDict()
        self.mutation = asyncio.Lock()
        self.diagnostic_lock = asyncio.Lock()
        self.maintenance_task = None
        self.mounted_digest = None
        # Remove the obsolete bootstrap credential when upgrading from v0.1.0.
        (self.store.root / "setup-code").unlink(missing_ok=True)

    async def install_certificate(self, cert, key, domain, source):
        async with self.mutation:
            config = self.store.get("config")
            if not config or config["domain"] != domain:
                raise Problem("配置域名已改变，本次证书未启用。请重新为当前域名申请或导入。")
            if self.gateway.servers and self.gateway.config["domain"] != domain:
                raise Problem("正在运行的网关使用另一个域名。请先停止网关，再安装新域名的证书。")
            metadata = await asyncio.to_thread(self.certificates.stage, cert, key, domain, source)
            context = await asyncio.to_thread(self.certificates.context, domain, metadata)
            self.certificates.commit(metadata)
            if self.gateway.servers:
                await self.gateway.apply(self.gateway.config, context)
            self.gateway.event("域名证书已更新，新连接将使用新证书。")

    async def load_mounted(self):
        config = self.store.get("config")
        if not config:
            raise Problem("请先保存连接配置。")
        try:
            cert = Path("/certs/fullchain.pem").read_bytes()
            key = Path("/certs/privkey.pem").read_bytes()
        except OSError:
            raise Problem("未找到挂载证书。请将 NAS 证书目录只读挂载到 /certs，并包含 fullchain.pem 与 privkey.pem。") from None
        digest = hashlib.sha256(cert + key).hexdigest()
        if digest != self.mounted_digest:
            await self.install_certificate(cert, key, config["domain"], "mounted")
            self.mounted_digest = digest

    async def maintenance(self):
        next_renewal = 0
        last_mount_error = None
        while True:
            await asyncio.sleep(60)
            metadata = self.certificates.metadata()
            if metadata and metadata.get("source") == "mounted":
                try:
                    await self.load_mounted()
                    last_mount_error = None
                except (Problem, OSError) as error:
                    message = error.message if isinstance(error, Problem) else "挂载证书暂时无法读取，保留当前证书。"
                    if message != last_mount_error:
                        self.gateway.event(message, "warning")
                        last_mount_error = message
            if time.monotonic() >= next_renewal:
                next_renewal = time.monotonic() + 12 * 3600
                if metadata and metadata.get("source") == "acme" and metadata["days_left"] < 30:
                    try:
                        self.acme.start(metadata["domain"])
                    except Problem as error:
                        self.gateway.event(error.message, "warning")

    def authenticated(self, request):
        token = request.cookies.get("airport_session", "")
        now = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        return token in self.sessions

    def session_response(self, request):
        if len(self.sessions) >= 64:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.monotonic() + 12 * 3600
        response = web.json_response({"ok": True})
        response.set_cookie("airport_session", token, httponly=True, samesite="Strict", max_age=43200,
                            secure=request.secure or os.getenv("AIRPORT_SECURE_COOKIE") == "1", path="/")
        return response

    def check_rate(self, request):
        peer = request.remote or "unknown"
        if peer not in self.attempts:
            if len(self.attempts) >= 1024:
                self.attempts.popitem(last=False)
            self.attempts[peer] = deque()
        attempts = self.attempts[peer]
        now = time.monotonic()
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if len(attempts) >= 10:
            raise Problem("尝试次数过多，请 5 分钟后再试。", status=429)
        attempts.append(now)


def password_record(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise Problem("管理密码需要 12–128 个字符。", {"password": "至少 12 个字符。"})
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return {"salt": salt.hex(), "digest": digest.hex()}


def check_password(password, record):
    if not isinstance(password, str) or len(password) > 128:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]), n=16384, r=8, p=1)
    return hmac.compare_digest(digest.hex(), record["digest"])


def allowed_host(host):
    hostname = urlsplit("//" + host).hostname or ""
    if hostname in {"localhost", *os.getenv("AIRPORT_PANEL_HOSTS", "").split(",")}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback or private_address(hostname)
    except ValueError:
        return False


@web.middleware
async def guard(request, handler):
    service = request.app["service"]
    try:
        if not allowed_host(request.host):
            raise Problem("管理面板只接受 NAS 内网地址。使用自定义面板域名时，请配置 AIRPORT_PANEL_HOSTS。", status=403)
        peer = request.remote or ""
        try:
            local_peer = ipaddress.ip_address(peer).is_loopback or private_address(peer)
        except ValueError:
            local_peer = False
        if not local_peer:
            raise Problem("管理面板仅允许局域网访问。", status=403)
        if request.method not in ("GET", "HEAD"):
            origin = urlsplit(request.headers.get("Origin", ""))
            if origin.scheme not in ("http", "https") or origin.netloc != request.host:
                raise Problem("请求来源不匹配，请从面板页面操作。", status=403)
            if request.content_type != "application/json":
                raise Problem("请使用面板提交 JSON 配置。", status=415)
        public = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}
        if request.path.startswith("/api/") and request.path not in public and not service.authenticated(request):
            raise Problem("请先登录管理面板。", status=401)
        response = await handler(request)
    except Problem as error:
        response = web.json_response({"error": error.message, "fields": error.fields}, status=error.status)
    except (ValueError, UnicodeDecodeError):
        response = web.json_response({"error": "请求格式不正确，请检查输入。", "fields": {}}, status=400)
    except web.HTTPException as error:
        response = web.json_response({"error": "请求过大或地址不存在。", "fields": {}}, status=error.status)
    except Exception:
        service.gateway.event("操作未完成，请检查数据目录权限和服务状态。", "error")
        response = web.json_response({"error": "操作未完成，原有连接配置可能仍在运行。请刷新查看状态。", "fields": {}}, status=500)
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                             "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
    return response


async def body(request):
    data = await request.json()
    if not isinstance(data, dict):
        raise Problem("请求内容必须是一个对象。")
    return data


async def auth_status(request):
    s = request.app["service"]
    return web.json_response({"setup_required": not bool(s.store.get("auth")), "authenticated": s.authenticated(request)})


async def setup(request):
    s = request.app["service"]
    s.check_rate(request)
    data = await body(request)
    async with s.mutation:
        if s.store.get("auth"):
            raise Problem("已完成初始化，请直接登录。", status=409)
        record = await asyncio.to_thread(password_record, data.get("password"))
        s.store.put("auth", record)
    return s.session_response(request)


async def login(request):
    s = request.app["service"]
    s.check_rate(request)
    data = await body(request)
    record = s.store.get("auth")
    if not record or not await asyncio.to_thread(check_password, data.get("password"), record):
        raise Problem("管理密码不正确。", {"password": "密码不正确。"}, 401)
    return s.session_response(request)


async def logout(request):
    s = request.app["service"]
    s.sessions.pop(request.cookies.get("airport_session"), None)
    response = web.json_response({"ok": True})
    response.del_cookie("airport_session", path="/")
    return response


async def password(request):
    s = request.app["service"]
    s.check_rate(request)
    data = await body(request)
    if not await asyncio.to_thread(check_password, data.get("old_password"), s.store.get("auth")):
        raise Problem("当前密码不正确。", status=403)
    record = await asyncio.to_thread(password_record, data.get("password"))
    s.store.put("auth", record)
    s.sessions.clear()
    return s.session_response(request)


async def state(request):
    s = request.app["service"]
    return web.json_response({"version": __version__, "config": s.store.get("config"),
                              "gateway": s.gateway.status(), "certificate": s.certificates.metadata(),
                              "acme": s.acme.settings(), "certificate_job": s.acme.job,
                              "providers": PROVIDERS, "panel_port": s.panel_port})


async def config(request):
    s = request.app["service"]
    data = validate_config(await body(request), s.panel_port)
    if request.path.endswith("validate"):
        return web.json_response({"ok": True, "config": data})
    if data["android_host"] in (s.gateway.host,) and data["android_control_port"] in s.gateway.ports.values():
        raise Problem("后端不能指向网关自身，请填写安卓地址和原始端口。")
    async with s.mutation:
        s.store.put("config", data)
    return web.json_response({"ok": True, "config": data, "message": "已保存。点击“应用并启动”后网关才会使用这份配置。"})


async def certificate_upload(request):
    s = request.app["service"]
    data = await body(request)
    config = s.store.get("config")
    if not config:
        raise Problem("请先保存连接配置。")
    cert, key = data.get("certificate"), data.get("private_key")
    if not isinstance(cert, str) or not isinstance(key, str):
        raise Problem("请同时选择证书链与私钥文件。")
    await s.install_certificate(cert.encode(), key.encode(), config["domain"], "upload")
    return web.json_response({"ok": True, "message": "证书已验证并安装。"})


async def certificate_mounted(request):
    await request.app["service"].load_mounted()
    return web.json_response({"ok": True, "message": "已加载挂载证书，将每分钟检查续期变化。"})


async def acme_settings(request):
    s = request.app["service"]
    config = s.store.get("config")
    if not config:
        raise Problem("请先保存连接配置。")
    s.acme.configure(await body(request), config["domain"])
    return web.json_response({"ok": True, "message": "DNS 证书设置已保存，凭据不会返回浏览器。"})


async def acme_start(request):
    s = request.app["service"]
    config = s.store.get("config")
    if not config:
        raise Problem("请先保存连接配置。")
    s.acme.start(config["domain"])
    return web.json_response({"ok": True, "message": "证书申请已开始。"}, status=202)


async def gateway_start(request):
    s = request.app["service"]
    async with s.mutation:
        config = s.store.get("config")
        if not config:
            raise Problem("请先保存连接配置。")
        context = await asyncio.to_thread(s.certificates.context, config["domain"])
        await s.gateway.apply(config, context)
        s.store.put("active", {"enabled": True, "config": config})
    return web.json_response({"ok": True, "message": "两路 TLS 转发已启动。下一步配置路由器，并用蜂窝网络测试。"})


async def gateway_stop(request):
    s = request.app["service"]
    async with s.mutation:
        s.store.put("active", {"enabled": False})
        await s.gateway.stop()
    return web.json_response({"ok": True, "message": "网关已停止。"})


async def diagnostic(request):
    s = request.app["service"]
    config = s.store.get("config")
    if not config:
        raise Problem("请先保存连接配置。")
    if s.diagnostic_lock.locked():
        raise Problem("正在检查，请等待本次完成。", status=409)
    data = await body(request)
    async with s.diagnostic_lock:
        return web.json_response(await diagnose(config, s.certificates.metadata(), data.get("public") is True))


async def export(request):
    s = request.app["service"]
    response = web.json_response({"airport_version": __version__, "config": s.store.get("config"),
                                  "note": "仅连接配置；不包含私钥、DNS 凭据、密码或配对密钥。"}, dumps=__import__("json").dumps)
    response.headers["Content-Disposition"] = 'attachment; filename="airport-config.json"'
    return response


async def health(request):
    return web.json_response({"ok": True, "version": __version__})


async def static(request):
    name = request.match_info.get("name", "index.html")
    if name not in ("index.html", "app.js", "style.css", "icon.svg"):
        raise web.HTTPNotFound()
    return web.FileResponse(STATIC / name)


async def lifecycle(app):
    s = app["service"]
    active = s.store.get("active", {})
    if active.get("enabled"):
        try:
            config = validate_config(active["config"], s.panel_port)
            context = await asyncio.to_thread(s.certificates.context, config["domain"])
            await s.gateway.apply(config, context)
        except Exception:
            s.gateway.event("自动启动失败：请检查已保存的证书、配置和监听端口，再手动启动。", "error")
    s.maintenance_task = asyncio.create_task(s.maintenance())
    yield
    s.maintenance_task.cancel()
    await asyncio.gather(s.maintenance_task, return_exceptions=True)
    await s.acme.close()
    await s.gateway.stop()


def create_app(data_dir=None, **options):
    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    app["service"] = Service(data_dir or os.getenv("AIRPORT_DATA", "/data"), **options)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes([
        web.get("/healthz", health), web.get("/api/auth/status", auth_status),
        web.post("/api/auth/setup", setup), web.post("/api/auth/login", login),
        web.post("/api/auth/logout", logout), web.post("/api/auth/password", password),
        web.get("/api/state", state), web.post("/api/config", config),
        web.post("/api/config/validate", config), web.post("/api/certificate/upload", certificate_upload),
        web.post("/api/certificate/mounted", certificate_mounted),
        web.post("/api/acme/settings", acme_settings), web.post("/api/acme/start", acme_start),
        web.post("/api/gateway/start", gateway_start), web.post("/api/gateway/stop", gateway_stop),
        web.post("/api/diagnostics", diagnostic), web.get("/api/export", export),
        web.get("/", static), web.get("/{name}", static),
    ])
    return app
