import asyncio
import copy
import socket
from collections import deque
from datetime import datetime, timezone

from .config import Problem


class Gateway:
    def __init__(self, host="0.0.0.0", control_port=17595, pcm_port=17596):
        self.host = host
        self.ports = {"control": control_port, "pcm": pcm_port}
        self.servers = []
        self.context = None
        self.config = None
        self.tasks = set()
        self.active = {"control": 0, "pcm": 0}
        self.bytes = {"up": 0, "down": 0}
        self.events = deque(maxlen=40)
        self.lock = asyncio.Lock()

    def event(self, text, level="info"):
        self.events.appendleft({"time": datetime.now(timezone.utc).isoformat(), "level": level, "message": text})

    def status(self):
        return {"running": bool(self.servers), "config": self.config, "active": dict(self.active),
                "bytes": dict(self.bytes), "events": list(self.events), "listen_host": self.host,
                "ports": dict(self.ports)}

    async def apply(self, config, context):
        async with self.lock:
            if self.servers:
                self.context = context
                self.config = copy.deepcopy(config)
                self.event("新配置已生效，已有连接继续使用建立时的目标。")
                return
            servers = []
            previous_context = self.context
            self.context = context

            def select_context(ssl_socket, server_name, initial):
                ssl_socket.context = self.context

            context.set_servername_callback(select_context)
            try:
                for channel, port in self.ports.items():
                    # Explicit V6ONLY=0 enables dual-stack for :: on Linux NAS hosts.
                    family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
                    sock = socket.socket(family, socket.SOCK_STREAM)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        if family == socket.AF_INET6:
                            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        sock.bind((self.host, port))
                        sock.setblocking(False)
                        server = await asyncio.start_server(
                            lambda r, w, c=channel: self.accept(r, w, c), sock=sock,
                            ssl=context, ssl_handshake_timeout=10, ssl_shutdown_timeout=5,
                            start_serving=False,
                        )
                        servers.append(server)
                    except BaseException:
                        sock.close()
                        raise
                self.config = copy.deepcopy(config)
                for server in servers:
                    await server.start_serving()
                self.servers = servers
                self.event("两路 TLS 网关已启动。请继续在蜂窝网络上验证。")
            except OSError as error:
                for server in servers:
                    server.close()
                    await server.wait_closed()
                self.context = previous_context
                raise Problem("网关端口无法监听，可能被其他服务占用，或 NAS 不支持所选监听地址。检查部署端口设置。") from error
            except BaseException:
                for server in servers:
                    server.close()
                    await server.wait_closed()
                self.context = previous_context
                raise

    def accept(self, reader, writer, channel):
        if not self.servers or len(self.tasks) >= 128:
            writer.close()
            return
        config = copy.deepcopy(self.config)
        task = asyncio.create_task(self.forward(reader, writer, channel, config))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def forward(self, reader, writer, channel, config):
        backend = None
        pumps = []
        self.active[channel] += 1
        try:
            port = config["android_control_port" if channel == "control" else "android_pcm_port"]
            upstream, backend = await asyncio.wait_for(asyncio.open_connection(config["android_host"], port), 6)

            async def pump(source, target, counter):
                while data := await asyncio.wait_for(source.read(65536), 120):
                    target.write(data)
                    await asyncio.wait_for(target.drain(), 30)
                    self.bytes[counter] += len(data)

            pumps = [asyncio.create_task(pump(reader, backend, "up")),
                     asyncio.create_task(pump(upstream, writer, "down"))]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (OSError, asyncio.TimeoutError):
            # Never record request bytes, caller numbers, HMAC headers or peer credentials.
            label = "控制" if channel == "control" else "音频"
            self.event(f"{label}连接中断或目标不可达，请检查安卓在线状态、端口与网络。", "warning")
        finally:
            # Finish bookkeeping and close both transports before any cancellable await.
            # stop() may cancel a task already in this cleanup block.
            self.active[channel] -= 1
            for connection in (writer, backend):
                if connection:
                    connection.close()
            for task in pumps:
                task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, backend):
                if connection:
                    try:
                        await asyncio.wait_for(connection.wait_closed(), 3)
                    except (OSError, asyncio.TimeoutError):
                        pass

    async def stop(self):
        async with self.lock:
            for server in self.servers:
                server.close()
            for server in self.servers:
                await server.wait_closed()
            self.servers = []
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.event("网关已停止，现有转发连接已关闭。")
