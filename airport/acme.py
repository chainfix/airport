import asyncio
import os
import re
import shutil
from datetime import datetime, timezone

from .config import Problem, domain_name

PROVIDERS = {
    "cloudflare": {"label": "Cloudflare", "fields": ["CF_DNS_API_TOKEN"]},
    "alidns": {"label": "阿里云 DNS", "fields": ["ALICLOUD_ACCESS_KEY", "ALICLOUD_SECRET_KEY"]},
    "tencentcloud": {"label": "腾讯云 DNSPod", "fields": ["TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"]},
    "dnspod": {"label": "DNSPod 旧版个人令牌", "fields": ["DNSPOD_API_KEY"]},
}


class ACME:
    def __init__(self, store, install):
        self.store, self.install = store, install
        self.task = None
        self.job = {"state": "idle", "message": "尚未申请证书。"}
        self.last_attempt = None

    def settings(self):
        settings = self.store.get("acme", {})
        return {"provider": settings.get("provider", "cloudflare"), "email": settings.get("email", ""),
                "domain": settings.get("domain", ""), "configured": bool(settings),
                "credentials_saved": bool(settings.get("credentials")), "available": bool(shutil.which("lego"))}

    def configure(self, data, domain):
        if self.task and not self.task.done():
            raise Problem("证书任务正在进行，请等待完成后再修改。", status=409)
        provider = data.get("provider")
        if provider not in PROVIDERS:
            raise Problem("请选择支持的 DNS 服务商。")
        email = str(data.get("email", "")).strip()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            raise Problem("请填写有效邮箱，用于证书服务账户。", {"email": "邮箱格式不正确。"})
        if data.get("agree") is not True:
            raise Problem("自动申请需要同意 Let's Encrypt 服务条款。")
        previous = self.store.get("acme", {})
        supplied = data.get("credentials", {})
        if not isinstance(supplied, dict):
            raise Problem("DNS 凭据格式不正确。")
        credentials = {}
        for name in PROVIDERS[provider]["fields"]:
            value = supplied.get(name, "")
            if not isinstance(value, str):
                raise Problem("DNS 凭据必须是文本。")
            value = value.strip()
            if not value and previous.get("provider") == provider:
                value = previous.get("credentials", {}).get(name, "")
            if not value or len(value) > 4096 or "\n" in value or "\x00" in value:
                raise Problem("请补全 DNS 服务商凭据；已有凭据可以留空保留。")
            credentials[name] = value
        self.store.put("acme", {"provider": provider, "email": email, "domain": domain_name(domain), "credentials": credentials})

    def start(self, domain):
        if self.task and not self.task.done():
            raise Problem("证书申请正在进行，请稍候。", status=409)
        if not shutil.which("lego"):
            raise Problem("未安装证书申请组件。请使用官方 Docker 镜像，或先导入 NAS 已有证书。")
        settings = self.store.get("acme")
        if not settings or settings.get("domain") != domain:
            raise Problem("请先为当前域名保存 DNS 证书设置。")
        if self.last_attempt and (datetime.now(timezone.utc) - self.last_attempt).total_seconds() < 60:
            raise Problem("请至少等待一分钟再重试，避免触发证书服务频率限制。", status=429)
        self.last_attempt = datetime.now(timezone.utc)
        self.job = {"state": "running", "message": "正在完成 DNS 验证，通常需要 1–5 分钟；可以留在页面等待。"}
        self.task = asyncio.create_task(self.run(settings))

    async def run(self, settings):
        process = None
        try:
            domain = settings["domain"]
            folder = self.store.root / "acme" / settings["provider"]
            cert = folder / "certificates" / f"{domain}.crt"
            key = folder / "certificates" / f"{domain}.key"
            args = ["lego", "--path", str(folder), "--email", settings["email"], "--accept-tos",
                    "--dns", settings["provider"], "--domains", domain]
            args += ["renew", "--days", "30"] if cert.exists() else ["run"]
            # Pass only this provider's credentials; never shell-interpolate input or echo lego output.
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR")}
            env.update(settings["credentials"])
            process = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.DEVNULL,
                                                           stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            await asyncio.wait_for(process.wait(), 600)
            if process.returncode or not cert.exists() or not key.exists():
                raise Problem("申请失败：检查 DNS 服务商是否选对、令牌权限、域名托管区域和 NAS 出网。证书服务限流时请稍后再试。")
            await self.install(cert.read_bytes(), key.read_bytes(), domain, "acme")
            self.job = {"state": "success", "message": "证书已安装。启用自动续期，运行中的网关已加载新证书。"}
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.job = {"state": "error", "message": "DNS 验证超过 10 分钟，请检查 DNS API 凭据和解析传播后重试。"}
        except Problem as error:
            self.job = {"state": "error", "message": error.message}
        except Exception:
            self.job = {"state": "error", "message": "证书任务未完成，请检查数据目录权限和网络。原证书保持不变。"}
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
