#!/bin/sh
# Download first, then run: sh install-airport.sh
# Only installs airport; does not change router, firewall or existing services.
set -eu
fail() { printf '\n错误：%s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail '请先在 NAS 安装 Docker / Container Manager。'
docker info >/dev/null 2>&1 || fail '无法连接 Docker。请使用具备 Docker 权限的 NAS 账号。'
docker compose version >/dev/null 2>&1 || fail '需要 Docker Compose v2 或更新版本。'
command -v curl >/dev/null 2>&1 || fail '请先安装 curl，或使用 README 中的 NAS 图形化导入方式。'

install_dir=${AIRPORT_INSTALL_DIR:-"$PWD/airport"}
[ ! -e "$install_dir" ] || fail "安装目录已经存在：$install_dir。为避免覆盖，请选择新目录；更新已有安装请看 README。"
nas_ip=${AIRPORT_NAS_IP:-}
if [ -z "$nas_ip" ]; then
    printf '填写 NAS 局域网 IPv4 地址（例如 192.168.1.20）：'
    if [ -r /dev/tty ]; then read -r nas_ip < /dev/tty; else fail '无人值守安装请设置 AIRPORT_NAS_IP。'; fi
fi
printf '%s\n' "$nas_ip" | awk -F. '
    NF != 4 { exit 1 }
    { for(i=1;i<=4;i++) if($i !~ /^[0-9]+$/ || $i<0 || $i>255) exit 1;
      if(!($1==10 || ($1==192 && $2==168) || ($1==172 && $2>=16 && $2<=31) || ($1==100 && $2>=64 && $2<=127))) exit 1; }
' || fail '请填写有效的局域网 IPv4 地址，不要填公网地址或域名。'
mode=${AIRPORT_NETWORK_MODE:-}
if [ -z "$mode" ]; then
    printf '部署方式：1=普通 IPv4 端口映射，2=Linux NAS IPv6 / 主机网络 [1]：'
    if [ -r /dev/tty ]; then read -r mode < /dev/tty; else mode=1; fi
fi
case "$mode" in ''|1|bridge) compose_file=compose.yaml ;; 2|host) compose_file=compose.host.yaml ;; *) fail '部署方式只能选 1 或 2。' ;; esac

task_tmp=$(mktemp -d)
trap 'rm -rf "$task_tmp"' EXIT HUP INT TERM
printf '\n正在下载 airport v0.1.1…\n'
curl -fL --retry 3 'https://github.com/chainfix/airport/archive/refs/tags/v0.1.1.tar.gz' -o "$task_tmp/source.tar.gz"
mkdir -p "$install_dir"
tar -xzf "$task_tmp/source.tar.gz" --strip-components=1 -C "$install_dir"
cd "$install_dir"
printf 'AIRPORT_PANEL_BIND=%s\nAIRPORT_PANEL_HOSTS=\n' "$nas_ip" > .env
chmod 600 .env
printf '%s\n' "$compose_file" > .compose-file
printf '\n正在拉取镜像；若暂时无法拉取，将尝试从源码构建。\n'
if docker compose -f "$compose_file" pull; then
    docker compose -f "$compose_file" up -d --no-build
else
    docker compose -f "$compose_file" up -d --build
fi
printf '\n面板地址：http://%s:8787\n' "$nas_ip"
printf '首次访问会直接提示设置管理密码，无需初始化码。\n'
printf '请不要把管理面板端口映射到公网。\n'
