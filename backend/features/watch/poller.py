"""Background poller: fetch the device list for every network and run watch.poll().

Started from the app lifespan. In demo mode it polls the synthetic device
list so the Watch tab is exercised without an eero login.
"""

from __future__ import annotations

import asyncio
import logging

from core import client as core_client
from core.demo import is_demo_mode

from . import config, service, store

logger = logging.getLogger("eero.watch")

_task: asyncio.Task | None = None


async def poll_all() -> list[dict]:
    """One pass over every network. Safe to call from a request handler."""
    results = []
    if is_demo_mode():
        from core import demo_data
        results.append(service.poll(demo_data.NETWORK_ID, demo_data.DEVICES))
        return results

    client = await core_client.ensure_client()
    if not client.is_authenticated:
        logger.debug("watch: not authenticated, skipping poll")
        return results

    from features.devices import service as devices_service
    from features.networks import service as networks_service

    nets = (await networks_service.list_networks(client)).get("networks", [])
    for net in nets:
        network_id = _network_id(net)
        if not network_id:
            continue
        payload = await devices_service.list_devices(client, network_id)
        results.append(service.poll(network_id, payload.get("devices", [])))
    return results


def _network_id(net: dict) -> str:
    if net.get("id") is not None:
        return str(net["id"])
    url = str(net.get("url") or "").rstrip("/")
    return url.split("/")[-1] if url else ""


async def _loop() -> None:
    await asyncio.sleep(5)  # let the app finish starting
    while True:
        try:
            await poll_all()
            store.prune_sightings(30)
            store.prune_events(90)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep polling through transient upstream failures
            logger.warning("watch poll failed: %s", e)
        await asyncio.sleep(config.poll_seconds())


def start() -> None:
    global _task
    if not config.enabled() or _task is not None:
        return
    _task = asyncio.get_event_loop().create_task(_loop())
    logger.info("watch poller started (every %ds)", config.poll_seconds())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
