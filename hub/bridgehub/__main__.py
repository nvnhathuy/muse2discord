import asyncio
import logging
import signal

from .config import Config
from .core import Hub
from .discord_runtime import DiscordRuntime
from .protocol import SocketServer


async def serve():
    config = Config.from_env()
    runtime = DiscordRuntime(config)
    hub = Hub(config, runtime)
    runtime.hub = hub
    server = SocketServer(hub)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = []
    try:
        await server.start()
        tasks = [asyncio.create_task(runtime.run()), asyncio.create_task(stop.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.close()
        await hub.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.error("hub stopped (%s); check configuration/storage", type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
