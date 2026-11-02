"""Watch feature: inventory baseline, new-device detection, steady-upload rule.

``poll()`` takes the eero device list (whatever the dashboard already fetches),
diffs it against the SQLite inventory, stores a usage sample per connected
device, and records events. Alert delivery (webhook) is a side effect of
events of type ``new_device`` and ``steady_upload``.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, probe, store

logger = logging.getLogger("eero.watch")


# --------------------------------------------------------------------------- helpers

def norm_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    parts = mac.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        return ":".join("%02x" % int(p, 16) for p in parts)
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usage(device: dict) -> tuple[float | None, float | None]:
    """Return (up, down) from whatever shape the eero API used."""
    u = device.get("usage") or {}
    if not isinstance(u, dict):
        return None, None
    up = _num(u.get("up"))
    if up is None:
        up = _num(u.get("up_mbps"))
    down = _num(u.get("down"))
    if down is None:
        down = _num(u.get("down_mbps"))
    return up, down


def _ip(device: dict) -> str:
    ip = device.get("ip")
    if not ip:
        ips = device.get("ips")
        if isinstance(ips, list) and ips:
            ip = ips[0]
    return str(ip or "")


def _fields(device: dict) -> dict[str, Any]:
    return {
        "ip": _ip(device),
        "hostname": device.get("hostname") or "",
        "nickname": device.get("nickname") or device.get("display_name") or "",
        "manufacturer": device.get("manufacturer") or "",
        "device_type": device.get("device_type") or "",
        "connection": device.get("connection_type") or ("wireless" if device.get("wireless") else ""),
        "device_url": device.get("url") or "",
    }


def _is_connected(device: dict) -> bool:
    c = device.get("connected")
    return True if c is None else bool(c)


def display_name(rec: dict) -> str:
    return rec.get("name") or rec.get("nickname") or rec.get("hostname") or rec.get("manufacturer") or rec["mac"]


# --------------------------------------------------------------------------- alerts

def send_webhook(title: str, body: str) -> None:
    url = config.webhook()
    if not url:
        return
    text = "%s\n%s" % (title, body)
    try:
        if "ntfy" in url:
            req = urllib.request.Request(url, data=text.encode(), method="POST",
                                         headers={"Title": title[:120]})
        else:
            req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # never let alert delivery break polling
        logger.warning("watch webhook failed: %s", e)


# --------------------------------------------------------------------------- core

def poll(network_id: str, devices: list[dict], *, first_run_trusts: bool = True) -> dict[str, Any]:
    """Diff the device list against the inventory. Returns a summary of what changed.

    On the very first poll for a network (empty inventory) every device is
    recorded as trusted when ``first_run_trusts`` is set: that is the baseline.
    """
    seen_at = store.now_iso()
    known = {d["mac"]: d for d in store.list_devices(network_id)}
    first_run = not known
    new, returned, sightings = [], [], []
    absent_days = config.absent_days()
    now = datetime.now(timezone.utc)

    for dev in devices:
        mac = norm_mac(dev.get("mac"))
        if not mac:
            continue
        if not _is_connected(dev):
            continue
        fields = _fields(dev)
        fields["trusted"] = 1 if (first_run and first_run_trusts) else 0
        prev = known.get(mac)
        created = store.upsert_device(mac, network_id, seen_at, fields)
        up, down = _usage(dev)
        sightings.append((mac, seen_at, fields["ip"], up, down))

        if created and not first_run:
            new.append(mac)
        elif prev and absent_days:
            try:
                gap = now - store.parse_iso(prev["last_seen"])
                if gap >= timedelta(days=absent_days):
                    returned.append((mac, gap.days))
            except ValueError:
                pass

    store.add_sightings(sightings)

    events = []
    if first_run:
        events.append(store.add_event(network_id, "baseline", "", "%d devices recorded as the baseline" % len(sightings)))

    for mac in new:
        rec = store.get_device(mac) or {"mac": mac}
        detail = "%s  %s  %s" % (rec.get("last_ip", ""), rec.get("manufacturer") or "unknown vendor",
                                 rec.get("hostname") or rec.get("nickname") or "")
        events.append(store.add_event(network_id, "new_device", mac, detail.strip()))
        send_webhook("New device on network", "%s  %s" % (mac, detail.strip()))

    for mac, days in returned:
        rec = store.get_device(mac) or {"mac": mac}
        events.append(store.add_event(network_id, "returned", mac,
                                      "%s back after %d days away" % (display_name(rec), days)))

    events.extend(_apply_upload_rule(network_id))

    summary = {
        "polled_at": seen_at,
        "online": len(sightings),
        "new": new,
        "returned": [m for m, _ in returned],
        "first_run": first_run,
        "events": events,
    }
    logger.info("watch poll %s: %d online, %d new", network_id, len(sightings), len(new))
    return summary


def _apply_upload_rule(network_id: str) -> list[dict]:
    """Flag devices uploading in nearly every sample over the last 24h.

    A steady, round-the-clock upload is the one network signature a streaming
    camera cannot hide. The event fires at most once per device per 24h.
    """
    stats = store.upload_stats(network_id, hours=24)
    min_up = config.upload_min()
    ratio = config.upload_ratio()
    min_samples = config.upload_samples()
    events = []
    for mac, s in stats.items():
        if s["samples"] < min_samples:
            continue
        uploading = sum(1 for u in s["ups"] if u > min_up)
        if uploading / s["samples"] < ratio:
            continue
        rec = store.get_device(mac)
        if rec is None or rec.get("trusted") and rec.get("notes", "").lower().startswith("ok:"):
            continue  # a note starting with "ok:" mutes the rule for a known uploader
        last = store.last_event_of(network_id, "steady_upload", mac)
        if last:
            try:
                if datetime.now(timezone.utc) - store.parse_iso(last["at"]) < timedelta(hours=24):
                    continue
            except ValueError:
                pass
        detail = "%s uploading in %d of %d samples over 24h (median %.0f)" % (
            display_name(rec), uploading, s["samples"], s["median_up"])
        events.append(store.add_event(network_id, "steady_upload", mac, detail))
        send_webhook("Steady upload", "%s  %s" % (mac, detail))
    return events


def summary(network_id: str) -> dict[str, Any]:
    devices = store.list_devices(network_id)
    now = datetime.now(timezone.utc)
    new_cutoff = now - timedelta(days=config.new_days())
    online_cutoff = now - timedelta(seconds=config.poll_seconds() * 2 + 60)
    stats = store.upload_stats(network_id, hours=24)

    for d in devices:
        d["display_name"] = display_name(d)
        try:
            d["is_new"] = store.parse_iso(d["first_seen"]) >= new_cutoff
            d["online"] = store.parse_iso(d["last_seen"]) >= online_cutoff
        except ValueError:
            d["is_new"] = d["online"] = False
        s = stats.get(d["mac"])
        if s:
            uploading = sum(1 for u in s["ups"] if u > config.upload_min())
            d["upload"] = {"samples": s["samples"], "uploading": uploading,
                           "ratio": round(uploading / s["samples"], 2), "median_up": s["median_up"]}
        else:
            d["upload"] = None

    untrusted = [d for d in devices if not d["trusted"]]
    new = [d for d in devices if d["is_new"]]
    uploaders = sorted((d for d in devices if d["upload"] and d["upload"]["samples"] >= config.upload_samples()),
                       key=lambda d: (d["upload"]["ratio"], d["upload"]["median_up"]), reverse=True)[:10]
    return {
        "counts": {
            "total": len(devices),
            "online": sum(1 for d in devices if d["online"]),
            "untrusted": len(untrusted),
            "untrusted_online": sum(1 for d in untrusted if d["online"]),
            "new": len(new),
        },
        "untrusted": untrusted,
        "new": new,
        "uploaders": uploaders,
        "events": store.list_events(network_id, limit=50),
        "config": {
            "poll_seconds": config.poll_seconds(),
            "new_days": config.new_days(),
            "webhook_configured": bool(config.webhook()),
            "probe_available": probe.available(),
            "lan_only": config.lan_only(),
            "allowed_cidrs": config.allowed_cidrs(),
        },
    }


async def probe_device(mac: str) -> dict[str, Any]:
    rec = store.get_device(mac)
    if rec is None:
        return {"ok": False, "error": "unknown device"}
    if not rec.get("last_ip"):
        return {"ok": False, "error": "device has no recorded IP"}
    result = await probe.probe(rec["last_ip"])
    if result.get("ok"):
        store.set_probe(mac, result)
    return result
