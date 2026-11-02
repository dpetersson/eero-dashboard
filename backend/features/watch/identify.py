"""Identify mode: find out which physical device a row in the inventory is.

Cloud-only smart switches expose nothing locally, so the only way to tie a MAC
to a wall plate is behaviour: cut power to one switch (air-gap tab, breaker,
unplug) and watch which device stops answering. This module probes a set of
targets every couple of seconds and reports which ones went dark.

Liveness is a TCP connect to port 80: an open port or a reset both prove the
device is up, a timeout means it is gone. That works for devices that drop
ICMP. Ping is tried as a fallback. The backend must sit on the same LAN as
the targets (host networking in Docker).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import socket
import subprocess
import sys
import time
from typing import Any, Callable

logger = logging.getLogger("eero.watch.identify")

INTERVAL_S = 2.0
TCP_TIMEOUT_S = 2.5           # some IoT stacks answer the SYN retransmit, about 1 s in
DARK_AFTER_MISSES = 2          # two consecutive misses (about 4 s) before a device counts as dark
AUTO_STOP_S = 30 * 60

_state: dict[str, Any] = {"active": False, "started": None, "network_id": None, "targets": {}, "task": None}
_prober: Callable[[str], bool] | None = None   # tests inject a fake
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="identify")


def _tcp_alive(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TCP_TIMEOUT_S)
    try:
        s.connect((ip, 80))
        return True
    except ConnectionRefusedError:
        return True            # RST: the stack is up, the port is just closed
    except (socket.timeout, TimeoutError):
        return False
    except OSError:
        return False           # no route / host down
    finally:
        s.close()


def _icmp_alive(ip: str) -> bool:
    cmd = ["ping", "-c", "1", "-q"] + (["-W", "700"] if sys.platform == "darwin" else ["-W", "1"]) + [ip]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _alive(ip: str) -> bool:
    if _prober is not None:
        return _prober(ip)
    return _tcp_alive(ip) or _icmp_alive(ip)


def set_prober(fn: Callable[[str], bool] | None) -> None:
    """Test hook: replace the network probe."""
    global _prober
    _prober = fn


async def probe_all() -> None:
    targets = _state["targets"]
    if not targets:
        return
    loop = asyncio.get_running_loop()
    ips = [t["ip"] for t in targets.values()]
    results = await asyncio.gather(*(loop.run_in_executor(_pool, _alive, ip) for ip in ips))
    now = time.time()
    for t, alive in zip(targets.values(), results):
        t["probes"] += 1
        if alive:
            t["last_alive"] = now
            if t["dark"]:
                t["dark"] = False
                t["dark_since"] = None
                t["returned_at"] = now
            t["misses"] = 0
        else:
            t["misses"] += 1
            # Only a device that was answering and then stopped is a signal. One that never
            # answered (asleep phone, TV in standby) is reported as never_answered instead.
            if not t["dark"] and t["last_alive"] is not None and t["misses"] >= DARK_AFTER_MISSES:
                t["dark"] = True
                t["dark_since"] = now
                logger.info("identify: %s (%s) went dark", t["mac"], t["ip"])


async def _loop() -> None:
    try:
        while _state["active"]:
            if time.time() - _state["started"] > AUTO_STOP_S:
                logger.info("identify: auto-stop after %d min", AUTO_STOP_S // 60)
                break
            try:
                await probe_all()
            except Exception as e:  # keep probing through transient errors
                logger.warning("identify probe failed: %s", e)
            await asyncio.sleep(INTERVAL_S)
    finally:
        _state["active"] = False
        _state["task"] = None


async def start(network_id: str, targets: list[dict[str, Any]], run_loop: bool = True) -> dict[str, Any]:
    """Begin probing. ``targets`` items need mac and ip; label is carried through."""
    await stop()
    now = time.time()
    _state.update({
        "active": True, "started": now, "network_id": network_id,
        "targets": {
            t["mac"]: {"mac": t["mac"], "ip": t["ip"], "label": t.get("label", ""), "vendor": t.get("vendor", ""),
                       "probes": 0, "misses": 0, "last_alive": None, "dark": False, "dark_since": None,
                       "returned_at": None}
            for t in targets if t.get("ip")
        },
    })
    if run_loop:
        _state["task"] = asyncio.get_running_loop().create_task(_loop())
    return status()


async def stop() -> None:
    task = _state.get("task")
    _state["active"] = False
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _state["task"] = None


def remove(mac: str) -> None:
    _state["targets"].pop(mac, None)


def status() -> dict[str, Any]:
    now = time.time()
    rows = []
    for t in _state["targets"].values():
        rows.append({
            "mac": t["mac"], "ip": t["ip"], "label": t["label"], "vendor": t["vendor"],
            "alive": not t["dark"] and t["last_alive"] is not None,
            "dark": t["dark"],
            "never_answered": t["last_alive"] is None and t["probes"] >= DARK_AFTER_MISSES,
            "dark_for_s": int(now - t["dark_since"]) if t["dark_since"] else None,
            "last_alive_s": int(now - t["last_alive"]) if t["last_alive"] else None,
            "probes": t["probes"],
        })
    def _ip_key(ip: str) -> tuple:
        try:
            a = ipaddress.ip_address(ip)
            return (a.version, int(a))
        except ValueError:
            return (9, ip)
    rows.sort(key=lambda r: (not r["dark"], -(r["dark_for_s"] or 0), _ip_key(r["ip"])))
    return {
        "active": bool(_state["active"]),
        "network_id": _state["network_id"],
        "started": _state["started"],
        "elapsed_s": int(now - _state["started"]) if _state["started"] and _state["active"] else None,
        "interval_s": INTERVAL_S,
        "auto_stop_s": AUTO_STOP_S,
        "targets": rows,
        "dark_count": sum(1 for r in rows if r["dark"]),
    }
