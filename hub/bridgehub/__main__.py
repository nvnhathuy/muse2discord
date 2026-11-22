import asyncio
import logging
import os
import signal
import threading

from .config import Config
from .core import Hub
from .discord_runtime import DiscordRuntime, RuntimeConnector
from .protocol import SocketServer

log = logging.getLogger(__name__)


def _start_integrated_admin(config, runtime, hub):
    """Integrated mode (PATCH-POINTS section 5): wire the shared registry
    into the hub and serve the admin page in its own thread.

    Returns (http_server, watcher) or (None, None) in legacy mode. The
    registry is the source of truth from here on; env only bootstrapped
    it (inside Config.from_env / bootstrap_state).
    """
    from hub_integration.bot_lifecycle import BotLifecycle
    from hub_integration.registry_sync import RegistryView, RegistryWatcher, bootstrap_state
    from hub_integration.routing_gate import RoutingGate
    from hub_integration.status_snapshot import DeliveryFacade, HubSnapshot

    registry, secrets = bootstrap_state(config.registry_dir, os.environ)
    view = RegistryView(registry, secrets)
    watcher = RegistryWatcher(view)
    gate = RoutingGate(watcher.agents)
    hub.routing_gate = gate
    hub.registry_watcher = watcher
    hub.bot_lifecycle = BotLifecycle(RuntimeConnector(runtime))
    # Rebuild the filter once with the gate attached (counts are still 0).
    hub._rebuild()

    from hub_admin.api import serve as admin_serve
    from hub_admin.app import build_app

    # The admin page reads the hub journal for its journal view.
    os.environ.setdefault("HUB_JOURNAL_PATH", config.journal_path)
    snapshot = HubSnapshot(
        hub,
        online_provider=lambda: sorted(
            key for key in watcher.agents
            if runtime.online.get(key) is not None and runtime.online[key].is_set()),
        expected_provider=lambda: sorted(watcher.agents),
    )
    delivery = DeliveryFacade(
        hub,
        ack_op=hub.admin_ack_on_behalf,
        reset_cursor_op=hub.admin_reset_cursor,
    )
    app = build_app(config.registry_dir, delivery=delivery,
                    status_provider=snapshot.status)
    host = os.environ.get("HUB_ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("HUB_ADMIN_PORT", "8899"))
    server = admin_serve(app, host, port)
    thread = threading.Thread(target=server.serve_forever, name="hub-admin", daemon=True)
    thread.start()
    log.info("integrated admin page on http://%s:%s/ (registry: %s)", host, port, config.registry_dir)
    return server, watcher


async def serve():
    config = Config.from_env()
    runtime = DiscordRuntime(config)
    hub = Hub(config, runtime)
    runtime.hub = hub
    hub._loop = asyncio.get_running_loop()
    server = SocketServer(hub)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    admin_server = None
    tasks = []
    try:
        await server.start()
        if config.registry_dir:
            admin_server, _watcher = _start_integrated_admin(config, runtime, hub)
            tasks.append(asyncio.create_task(hub.watch_registry()))
        tasks += [asyncio.create_task(runtime.run()), asyncio.create_task(stop.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if admin_server is not None:
            admin_server.shutdown()
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
