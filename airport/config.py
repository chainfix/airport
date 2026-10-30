import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path


class Problem(Exception):
    def __init__(self, message, fields=None, status=400):
        self.message, self.fields, self.status = message, fields or {}, status
        super().__init__(message)


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


PRIVATE_NETS = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
)))


def private_address(value):
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in net for net in PRIVATE_NETS)
    except ValueError:
        return False


def domain_name(value):
    if not isinstance(value, str):
        raise ValueError("请填写域名，例如 phone.example.com。")
    value = value.strip().lower()
    if any(char in value for char in "/:@* "):
        raise ValueError("只填域名，不要带 https://、端口、路径或通配符。")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("域名格式不正确。") from None
    if len(value) > 253 or "." not in value or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ValueError("请填完整域名，例如 phone.example.com。")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("这里需要与证书匹配的域名，不能填写 IP 地址。")


def port_number(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)):
        raise ValueError("端口必须是 1–65535 的整数。")
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError("端口必须是 1–65535 的整数。")
    return number


def validate_config(data, panel_port=8787):
    if not isinstance(data, dict):
        raise Problem("配置必须是一个对象。")
    errors, config = {}, {}
    try:
        config["domain"] = domain_name(data.get("domain", ""))
    except ValueError as error:
        errors["domain"] = str(error)
    host = str(data.get("android_host", "")).strip()
    if not private_address(host):
        errors["android_host"] = "填写安卓的局域网 IP（例如 192.168.1.50），不要填公网、域名或本机回环地址。"
    else:
        config["android_host"] = str(ipaddress.ip_address(host))
    for field, default in (("android_control_port", 7592), ("android_pcm_port", 7593),
                           ("public_control_port", 17595), ("public_pcm_port", 17596)):
        try:
            config[field] = port_number(data.get(field, default))
        except ValueError as error:
            errors[field] = str(error)
    for first, second in (("android_control_port", "android_pcm_port"),
                          ("public_control_port", "public_pcm_port")):
        if first in config and config.get(first) == config.get(second):
            errors[second] = "控制和音频需要使用两个不同的端口。"
    for field in ("public_control_port", "public_pcm_port"):
        if config.get(field) == panel_port:
            errors[field] = "请勿将管理面板端口用作公网转发入口。"
    if errors:
        raise Problem("还有几项需要修改，请查看输入框下方提示。", errors)
    return config


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def get(self, name, default=None):
        return read_json(self.root / f"{name}.json", default)

    def put(self, name, value):
        save_json(self.root / f"{name}.json", value)
