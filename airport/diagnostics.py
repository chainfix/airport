import asyncio
import ipaddress
import socket
import ssl

from .config import private_address


def result(name, status, message, detail=""):
    return {"name": name, "status": status, "message": message, "detail": detail}


async def check_dns(domain):
    try:
        records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(domain, None, type=socket.SOCK_STREAM), 6)
        addresses = sorted(set(record[4][0] for record in records))
        public = [a for a in addresses if ipaddress.ip_address(a).is_global]
        if not public:
            return result("域名解析", "warning", "域名只解析到内网地址，无法据此从外网连接。", "、".join(addresses))
        versions = set(ipaddress.ip_address(a).version for a in public)
        detail = "、".join(addresses)
        if versions == {6}:
            detail += "；仅 IPv6，手机网络也必须支持 IPv6。"
        elif versions == {4, 6}:
            detail += "；IPv4 和 IPv6 都需要配置正确的入站规则。"
        return result("域名解析", "ok", "已找到公网地址，请核对是否为当前入口地址。", detail)
    except (OSError, asyncio.TimeoutError):
        return result("域名解析", "error", "域名无法解析，请检查拼写、DDNS 更新状态和 NAS 的 DNS。")


async def check_control(host, port, *, tls_domain=None):
    name = "公网控制入口（NAS 视角）" if tls_domain else "安卓控制接口"
    writer = None
    try:
        context = ssl.create_default_context() if tls_domain else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=context, server_hostname=tls_domain if context else None,
        ), 6)
        http_host = tls_domain or host
        writer.write(f"GET /v1/health HTTP/1.1\r\nHost: {http_host}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 6)
        lines = headers.decode("latin1").split("\r\n")
        status = int(lines[0].split()[1])
        lower = headers.lower()
        if b"www-authenticate: basic" in lower:
            return result(name, "error", "这里是网页账号密码登录入口，不是 AirSIM 控制入口。", "检查是否误填 NAS 管理端口或普通网页反向代理端口。")
        if status in (401, 403):
            return result(name, "warning", "HTTP 接口可达，返回鉴权要求；仍需 iPhone 验证设备身份。", "网关不持有配对密钥，不能凭 401/403 判断目标一定是 AirSIM。")
        if status == 200:
            return result(name, "warning", "HTTP 接口可达，但尚未验证设备身份。", "请在 iPhone 点击“验证并保存”；网页也可能返回 200。")
        return result(name, "error", f"接口返回 HTTP {status}，请核对目标是否为安卓控制服务。")
    except ssl.SSLCertVerificationError:
        return result(name, "error", "证书验证失败，请检查域名、证书有效期和完整证书链。")
    except (OSError, asyncio.TimeoutError, ValueError, IndexError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return result(name, "error", "连接失败或目标不是 HTTP 控制接口。", "检查端口、安卓在线状态及防火墙。公网检查也可能受 NAT 回流限制。")
    finally:
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass


async def check_pcm(host, port, *, tls_domain=None):
    name = "公网音频入口（NAS 视角）" if tls_domain else "安卓音频端口"
    writer = None
    try:
        context = ssl.create_default_context() if tls_domain else None
        _, writer = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=context, server_hostname=tls_domain if context else None,
        ), 6)
        return result(name, "warning", "TLS 握手通过，音频鉴权待实测。" if context else "TCP 端口可达，音频协议待实测。",
                      "未发送 PCM 鉴权或音频数据；需要实际通话确认双向声音。")
    except ssl.SSLCertVerificationError:
        return result(name, "error", "音频证书校验失败，不能使用明文音频端口代替。")
    except (OSError, asyncio.TimeoutError):
        return result(name, "error", "音频端口无法连接。", "检查是否使用 TCP TLS 转发，以及路由器、NAS 和安卓端口。")
    finally:
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, asyncio.TimeoutError):
                pass


async def diagnose(config, certificate, public=False):
    checks = [check_dns(config["domain"]),
              check_control(config["android_host"], config["android_control_port"]),
              check_pcm(config["android_host"], config["android_pcm_port"])]
    if public:
        # Resolve once, reject loopback/link-local/metadata targets, then pin the address for each probe.
        # The TLS SNI and Host header still use the configured domain.
        try:
            records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                config["domain"], None, type=socket.SOCK_STREAM), 6)
            addresses = sorted(set(record[4][0] for record in records))
            addresses = [a for a in addresses if ipaddress.ip_address(a).is_global or private_address(a)]
            for host in addresses[:4]:
                checks += [check_control(host, config["public_control_port"], tls_domain=config["domain"]),
                           check_pcm(host, config["public_pcm_port"], tls_domain=config["domain"])]
        except (OSError, asyncio.TimeoutError):
            pass
    values = await asyncio.gather(*checks)
    if not certificate:
        values.insert(0, result("域名证书", "error", "尚未配置证书，请先申请或导入。"))
    elif certificate["domain"] != config["domain"]:
        values.insert(0, result("域名证书", "error", "当前配置域名与已导入证书的验证域名不同，请重新导入或申请。"))
    else:
        days = certificate["days_left"]
        values.insert(0, result("域名证书", "error" if days < 0 else "warning" if days < 30 else "ok",
                                f"证书剩余 {days} 天。", "证书状态不代表公网端口可达。"))
    return {"checks": values, "scope": "检查从 NAS 发起，不证明公网可达。请关闭手机 Wi-Fi 后完成最终验证。",
            "tested_public": public}
