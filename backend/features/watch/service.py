"""Watch feature: inventory baseline, new-device detection, steady-upload rule.

``poll()`` takes the eero device list (whatever the dashboard already fetches),
diffs it against the SQLite inventory, stores a usage sample per connected
device, and records events. Alert delivery (webhook) is a side effect of
events of type ``new_device`` and ``steady_upload``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, lanscan, probe, store

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


def _segment(device: dict) -> str:
    if device.get("is_guest"):
        return "guest"
    for key in ("vlan_name", "subnet_kind"):
        v = device.get(key)
        if v and str(v).lower() not in ("main", "primary", "default"):
            return str(v)
    return ""


def _fields(device: dict) -> dict[str, Any]:
    make = " ".join(str(device.get(k) or "") for k in ("inferred_make", "inferred_model")).strip()
    if not make and device.get("model_name"):
        make = str(device["model_name"])
    mac = norm_mac(device.get("mac"))
    return {
        "ip": _ip(device),
        "hostname": device.get("hostname") or "",
        "nickname": device.get("nickname") or device.get("display_name") or "",
        "manufacturer": device.get("manufacturer") or "",
        "device_type": device.get("device_type") or "",
        "connection": device.get("connection_type") or ("wireless" if device.get("wireless") else ""),
        "device_url": device.get("url") or "",
        "make_model": make,
        "ssid": device.get("ssid") or "",
        "segment": _segment(device),
        "oui_vendor": lanscan.vendor_for(mac) if mac else "",
    }


def _is_connected(device: dict) -> bool:
    c = device.get("connected")
    return True if c is None else bool(c)


def _eero_ts(value: Any) -> str | None:
    """eero's last_active/first_active ('2026-11-02T14:49:52.301Z') in our second-resolution ISO form."""
    if not value:
        return None
    try:
        return store.parse_iso(str(value).replace("Z", "+00:00")).replace(microsecond=0).isoformat()
    except ValueError:
        return None


def nodes_as_devices(nodes: list[dict]) -> list[dict]:
    """eero lists its own hardware under /eeros, not /devices. Fold the nodes in so a
    LAN sweep does not report the gateway as an unknown device."""
    out = []
    for n in nodes or []:
        mac = n.get("mac_address") or n.get("mac")
        if not mac:
            continue
        location = n.get("location") or n.get("nickname") or n.get("model") or "eero"
        out.append({
            "mac": mac,
            "ip": n.get("ip_address") or n.get("ip") or "",
            "hostname": "",
            "nickname": "eero %s" % location,
            "manufacturer": "eero",
            "device_type": "eero_node",
            "connection_type": n.get("connection_type") or ("wired" if n.get("wired") else "mesh"),
            "connected": str(n.get("status") or "green").lower() in ("green", "connected", "online", "true"),
            "url": n.get("url") or "",
            "inferred_make": "eero",
            "inferred_model": n.get("model") or "",
            "_watch_role": "eero_node",
        })
    return out


def display_name(rec: dict) -> str:
    return (rec.get("name") or rec.get("nickname") or rec.get("hostname") or rec.get("lan_hostname")
            or rec.get("manufacturer") or rec.get("make_model") or rec.get("oui_vendor") or rec["mac"])


def best_vendor(rec: dict) -> str:
    """eero's manufacturer first, then its inferred make/model, then the OUI table."""
    v = rec.get("manufacturer") or rec.get("make_model") or rec.get("oui_vendor") or ""
    return v


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

def baseline_at(network_id: str) -> str | None:
    """When the baseline was taken. Inventories created before the meta key existed
    fall back to the baseline event, and the key is written so later reads are cheap."""
    value = store.get_meta("baseline_at:%s" % network_id)
    if value:
        return value
    events = [e for e in store.list_events(network_id, limit=1000) if e["type"] == "baseline"]
    if not events:
        return None
    value = events[-1]["at"]
    store.set_meta("baseline_at:%s" % network_id, value)
    return value


def poll(network_id: str, devices: list[dict], *, first_run_trusts: bool = True) -> dict[str, Any]:
    """Diff the device list against the inventory. Returns a summary of what changed.

    On the very first poll for a network (empty inventory) every device is
    recorded as trusted when ``first_run_trusts`` is set: that is the baseline.
    """
    seen_at = store.now_iso()
    store.set_meta("network_id", network_id)
    known = {d["mac"]: d for d in store.list_devices(network_id)}
    first_run = not any(d.get("first_source", "eero") == "eero" for d in known.values())
    new, returned, sightings = [], [], []
    absent_days = config.absent_days()
    now = datetime.now(timezone.utc)

    for dev in devices:
        mac = norm_mac(dev.get("mac"))
        if not mac:
            continue
        connected = _is_connected(dev)
        fields = _fields(dev)
        fields["trusted"] = 1 if (first_run and first_run_trusts) else 0
        prev = known.get(mac)
        # Disconnected devices are still part of what eero knows: record them with eero's own
        # last-active time so a sleeping Mac seen on the wire is never "unknown to eero".
        stamp = seen_at if connected else (_eero_ts(dev.get("last_active")) or "")
        if not connected and not stamp and prev is None:
            continue  # nothing usable to record
        first = _eero_ts(dev.get("first_active"))
        created = store.upsert_device(mac, network_id, stamp or seen_at, fields, first_seen=first)
        if not connected:
            if created and not first_run:
                base = baseline_at(network_id)
                if base and stamp <= base:
                    # eero knew it before we started watching: household history, not a newcomer.
                    store.label_device(mac, trusted=True)
                else:
                    new.append(mac)
            elif first_run and first_run_trusts and prev is not None and not prev.get("trusted"):
                store.label_device(mac, trusted=True)
            continue
        up, down = _usage(dev)
        sightings.append((mac, seen_at, fields["ip"], up, down))

        if first_run and first_run_trusts and prev is not None and not prev.get("trusted"):
            # Seen by a LAN sweep before the first eero poll: part of the baseline too.
            store.label_device(mac, trusted=True)
        elif dev.get("_watch_role") == "eero_node" and (created or not prev.get("trusted")) and not (prev or {}).get("name"):
            # eero's own hardware is trusted by definition; no new-device alert for it.
            store.label_device(mac, trusted=True)
        elif created and not first_run:
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
        store.set_meta("baseline_at:%s" % network_id, seen_at)
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


# --------------------------------------------------------------------------- LAN scans

def ingest_lan_scan(network_id: str, scan: dict, source: str | None = None) -> dict[str, Any]:
    """Merge a LAN sweep (built-in or pushed by netscan.py) into the inventory.

    Devices not known at all are created untrusted with first_source=lan and
    raise a ``new_device_lan`` event: something answers on the wire that eero
    has never reported. That is worth a look even if it turns out benign.
    """
    seen_at = store.now_iso()
    source = source or scan.get("source") or "lan"
    net = scan.get("network") or {}
    cidr = net.get("cidr") or ""
    # eero nodes answer ARP with a different MAC than eero reports for them. Match the
    # wire-side interface by the node's IP, or by eero's OUI plus its eero-xxxx hostname.
    known = store.list_devices(network_id)
    node_by_ip = {d["last_ip"]: d for d in known if d.get("device_type") == "eero_node" and d.get("last_ip")}
    node_macs = {d["mac"] for d in known if d.get("device_type") == "eero_node"}
    new, rows = [], []
    for d in scan.get("devices") or []:
        mac = norm_mac(d.get("mac"))
        if not mac:
            continue
        ip = str(d.get("ip") or "")
        hostname = str(d.get("hostname") or "")
        vendor = str(d.get("vendor") or "") or lanscan.vendor_for(mac)
        if vendor.startswith("(randomised"):
            vendor = ""
        created = store.upsert_lan_device(mac, network_id, seen_at, ip, hostname, vendor, source)
        rows.append((mac, seen_at, ip, hostname, vendor, source))
        node = node_by_ip.get(ip)
        looks_like_node = vendor.lower().startswith("eero") and hostname.lower().startswith("eero-")
        if mac not in node_macs and (node or looks_like_node):
            rec = store.get_device(mac) or {}
            if created or not rec.get("trusted"):
                label = "%s (LAN interface)" % (node.get("nickname") or node.get("name") or "eero") if node else "eero node (LAN interface)"
                store.mark_eero_node(mac, label)
            continue
        if created:
            new.append(mac)
    store.add_lan_sightings(rows)
    store.set_meta("lan_scan:%s" % network_id, {
        "at": seen_at, "source": source, "cidr": cidr, "count": len(rows),
        "gateway_mac": net.get("gateway_mac") or "", "scanner_ip": net.get("scanner_ip") or "",
    })

    events = []
    for mac in new:
        rec = store.get_device(mac) or {"mac": mac}
        detail = "%s  %s  %s  (on the wire via %s, never reported by eero)" % (
            rec.get("lan_ip", ""), rec.get("oui_vendor") or "unknown vendor", rec.get("lan_hostname") or "", source)
        events.append(store.add_event(network_id, "new_device_lan", mac, detail.strip()))
        send_webhook("Device on LAN not reported by eero", "%s  %s" % (mac, detail.strip()))
    logger.info("watch lan scan %s from %s: %d devices, %d unknown to eero", network_id, source, len(rows), len(new))
    return {"scanned_at": seen_at, "source": source, "cidr": cidr, "count": len(rows), "new": new, "events": events}


def default_network_id() -> str | None:
    return config.network_id() or store.get_meta("network_id")


def _cross_reference(network_id: str, devices: list[dict]) -> dict[str, Any]:
    last = store.get_meta("lan_scan:%s" % network_id)
    out: dict[str, Any] = {
        "available": lanscan.available(),
        "builtin_enabled": config.lan_scan(),
        "oui_loaded": lanscan.oui_available(),
        "last_scan": last,
        "both": 0, "eero_only": 0, "lan_only": [], "vendor_filled": 0,
    }
    if not last:
        return out
    try:
        net = ipaddress.ip_network(last["cidr"]) if last.get("cidr") else None
    except ValueError:
        net = None
    now = datetime.now(timezone.utc)
    lan_window = now - timedelta(seconds=config.lan_scan_seconds() * 2 + 120)

    def in_subnet(ip: str) -> bool:
        if net is None or not ip:
            return True
        try:
            return ipaddress.ip_address(ip) in net
        except ValueError:
            return False

    for d in devices:
        lan_recent = False
        if d.get("lan_last_seen"):
            try:
                lan_recent = store.parse_iso(d["lan_last_seen"]) >= lan_window
            except ValueError:
                pass
        d["lan_online"] = lan_recent
        eero_on_subnet = d.get("online") and in_subnet(d.get("last_ip", ""))
        if d.get("device_type") == "eero_node":
            # Nodes are reported by eero under /eeros; their wire-side interface counts as matched.
            if lan_recent or eero_on_subnet:
                out["both"] += 1
            continue
        if lan_recent and eero_on_subnet:
            out["both"] += 1
        elif lan_recent and (d.get("first_source") == "lan" or not d.get("online")):
            out["lan_only"].append(d)
        elif eero_on_subnet and not lan_recent:
            out["eero_only"] += 1
        if not d.get("manufacturer") and (d.get("oui_vendor") or d.get("make_model")):
            out["vendor_filled"] += 1
    return out


def summary(network_id: str) -> dict[str, Any]:
    devices = store.list_devices(network_id)
    now = datetime.now(timezone.utc)
    new_cutoff = now - timedelta(days=config.new_days())
    online_cutoff = now - timedelta(seconds=config.poll_seconds() * 2 + 60)
    stats = store.upload_stats(network_id, hours=24)
    base = baseline_at(network_id)

    for d in devices:
        d["display_name"] = display_name(d)
        d["vendor"] = best_vendor(d)
        try:
            first = store.parse_iso(d["first_seen"])
            # "New" means joined after the baseline, not "baseline was taken recently".
            d["is_new"] = (d.get("device_type") != "eero_node" and first >= new_cutoff
                           and (not base or first > store.parse_iso(base)))
            d["online"] = bool(d.get("last_seen")) and store.parse_iso(d["last_seen"]) >= online_cutoff
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
    cross = _cross_reference(network_id, devices)
    return {
        "cross": cross,
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
            "tuya_file": __import__("features.watch.tuya_import", fromlist=["available"]).available(),
            "lan_scan_enabled": config.lan_scan(),
            "lan_scan_seconds": config.lan_scan_seconds(),
        },
    }


async def set_eero_nickname(network_id: str, rec: dict, name: str) -> dict[str, Any]:
    """Write the label to eero as the device nickname so the eero app shows it too."""
    from core import client as core_client
    from core.demo import is_demo_mode
    if is_demo_mode():
        return {"ok": False, "error": "demo mode: nothing is written to eero"}
    client = await core_client.ensure_client()
    if not client.is_authenticated:
        return {"ok": False, "error": "not logged in to eero"}
    device_id = (rec.get("device_url") or "").rstrip("/").split("/")[-1] or rec["mac"]
    from features.devices import service as devices_service
    try:
        await devices_service.set_device_nickname(client, network_id, device_id, name)
    except Exception as e:  # HTTPException from translate_errors or dry-run block
        detail = getattr(e, "detail", None)
        msg = detail.get("message") if isinstance(detail, dict) else (detail or str(e))
        return {"ok": False, "error": str(msg)}
    store.set_nickname(rec["mac"], name)
    return {"ok": True}


def identify_candidates(network_id: str, macs: list[str] | None = None) -> list[dict[str, Any]]:
    """Unnamed devices with an address, or an explicit MAC list. eero nodes are excluded."""
    s = summary(network_id)
    by_mac = {d["mac"]: d for d in store.list_devices(network_id)}
    for d in s["untrusted"] + s["new"] + s["uploaders"] + s["cross"]["lan_only"]:
        by_mac[d["mac"]] = d
    wanted = {norm_mac(m) for m in macs} if macs else None
    out = []
    for d in by_mac.values():
        ip = d.get("last_ip") or d.get("lan_ip")
        if not ip or d.get("device_type") == "eero_node":
            continue
        if wanted is not None:
            if d["mac"] not in wanted:
                continue
        elif d.get("name"):
            continue
        out.append({"mac": d["mac"], "ip": ip, "vendor": best_vendor(d),
                    "label": d.get("name") or d.get("nickname") or d.get("hostname") or d.get("lan_hostname") or ""})
    def _key(t: dict) -> tuple:
        try:
            addr = ipaddress.ip_address(t["ip"])
            return (addr.version, int(addr))
        except ValueError:
            return (9, t["ip"])
    return sorted(out, key=_key)


async def probe_device(mac: str) -> dict[str, Any]:
    rec = store.get_device(mac)
    if rec is None:
        return {"ok": False, "error": "unknown device"}
    ip = rec.get("last_ip") or rec.get("lan_ip")
    if not ip:
        return {"ok": False, "error": "device has no recorded IP"}
    result = await probe.probe(ip)
    if result.get("ok"):
        store.set_probe(mac, result)
    return result
