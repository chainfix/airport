import os
from aiohttp import web
from .web import create_app


def main():
    os.umask(0o077)
    panel_port = int(os.getenv("AIRPORT_PANEL_PORT", "8787"))
    app = create_app(host=os.getenv("AIRPORT_TLS_HOST", "0.0.0.0"),
                     control_port=int(os.getenv("AIRPORT_CONTROL_PORT", "17595")),
                     pcm_port=int(os.getenv("AIRPORT_PCM_PORT", "17596")), panel_port=panel_port)
    code = app["service"].setup_code
    if code:
        print("airport 首次初始化码（仅用于创建管理员，请勿分享）：" + code, flush=True)
    web.run_app(app, host=os.getenv("AIRPORT_PANEL_BIND", "0.0.0.0"), port=panel_port,
                access_log=None, print=lambda _: print(f"airport 管理面板已监听端口 {panel_port}", flush=True))


if __name__ == "__main__":
    main()
