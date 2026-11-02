"""Background poller: fetch the device list for every network and run watch.poll().

Started from the app lifespan. In demo mode it polls the synthetic device
list so the Watch tab is exercised without an eero login.
"""

from __future__ import annotations

import asyncio
import logging

from core import client as core_client
from core.demo import is_demo_mode

from . import config, lanscan, service, store

logger = logging.getLogger("eero.watch")

_task: asyncio.Task | None = None
_lan_task: asyncio.Task | None = None


async def poll_all() -> list[dict]:
    """One pass over every network. Safe to call from a request handler."""
    results = []
    if is_demo_mode():
        from core import demo_data
        devices = list(demo_data.DEVICES) + service.nodes_as_devices(demo_data.EERO_NODES)
        results.append(service.poll(demo_data.NETWORK_ID, devices))
        return results

    client = await core_client.ensure_client()
    if not client.is_authenticated:
        logger.debug("watch: not authenticated, skipping poll")
        return results
    store.set_meta("last_auth_ok", store.now_iso())

    from features.devices import service as devices_service
    from features.networks import service as networks_service

    nets = (await networks_service.list_networks(client)).get("networks", [])
    for net in nets:
        network_id = _network_id(net)
        if not network_id:
            continue
        payload = await devices_service.list_devices(client, network_id)
        devices = list(payload.get("devices", []))
        try:
            nodes = (await networks_service.list_eeros(client, network_id)).get("eeros", [])
            devices += service.nodes_as_devices(nodes)
        except Exception as e:  # nodes are a nicety; never block the device poll on them
            logger.warning("watch: could not list eero nodes: %s", e)
        results.append(service.poll(network_id, devices))
    return results


def _network_id(net: dict) -> str:
    if net.get("id") is not None:
        return str(net["id"])
    url = str(net.get("url") or "").rstrip("/")
    return url.split("/")[-1] if url else ""


async def lan_scan_once(network_id: str | None = None) -> dict | None:
    """Run the built-in sweep from this host and merge it. None when no network id is known yet."""
    network_id = network_id or service.default_network_id()
    if is_demo_mode() and not network_id:
        from core import demo_data
        network_id = demo_data.NETWORK_ID
    if not network_id:
        logger.info("watch lan scan: no network id yet (log in or set WATCH_NETWORK_ID)")
        return None
    home = store.get_meta("home_gateway_mac")
    scan = await lanscan.scan_async(config.lan_scan_subnet())
    gw = (scan.get("network") or {}).get("gateway_mac")
    if home and gw and gw != home:
        logger.warning("watch lan scan skipped: gateway %s is not the home gateway %s", gw, home)
        return {"skipped": True, "reason": "not on the home network"}
    if not home and gw:
        store.set_meta("home_gateway_mac", gw)
    return service.ingest_lan_scan(network_id, scan)


async def _loop() -> None:
    await asyncio.sleep(5)  # let the app finish starting
    while True:
        delay = config.poll_seconds()
        try:
            results = await poll_all()
            if not results and not is_demo_mode():
                delay = 30  # not logged in yet: check again soon so the baseline starts right after login
            store.prune_sightings(30)
            store.prune_lan_sightings(30)
            store.prune_events(90)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep polling through transient upstream failures
            logger.warning("watch poll failed: %s", e)
        await asyncio.sleep(delay)


async def _lan_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await lan_scan_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("watch lan scan failed: %s", e)
        await asyncio.sleep(config.lan_scan_seconds())


def start() -> None:
    global _task, _lan_task
    if not config.enabled() or _task is not None:
        return
    loop = asyncio.get_event_loop()
    _task = loop.create_task(_loop())
    logger.info("watch poller started (every %ds)", config.poll_seconds())
    if config.lan_scan():
        _lan_task = loop.create_task(_lan_loop())
        logger.info("watch built-in LAN scan started (every %ds)", config.lan_scan_seconds())


async def stop() -> None:
    global _task, _lan_task
    for t in (_task, _lan_task):
        if t is None:
            continue
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    _task = _lan_task = None
