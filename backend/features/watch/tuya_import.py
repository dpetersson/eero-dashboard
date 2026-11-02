"""Import device names from a tinytuya ``devices.json``.

Tuya-platform switches (Feit, Smart Life, Tuya Smart) carry their user-given
names only in Tuya's cloud. ``python -m tinytuya wizard`` writes those names,
device ids and (in current versions) MAC addresses to ``devices.json``, and
its optional poll writes ``snapshot.json`` with each device's LAN IP. Drop
both into ``data/tuya/`` and this module matches them to the inventory by
MAC, falling back to IP, so every switch can be named in one click.

Local keys in those files are never returned by the API or stored in the
inventory. They stay in the files for the local-control feature to read.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import store
from .service import norm_mac

logger = logging.getLogger("eero.watch.tuya")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
TUYA_DIR = DATA_DIR / "tuya"
DEVICES_FILE = "devices.json"
SNAPSHOT_FILE = "snapshot.json"

_dir_override: Path | None = None


def use_dir(path: Path | None) -> None:
    """Test hook."""
    global _dir_override
    _dir_override = path


def _dir() -> Path:
    return _dir_override or TUYA_DIR


def available() -> bool:
    return (_dir() / DEVICES_FILE).exists()


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("tuya import: cannot read %s: %s", path, e)
        return None


def load() -> list[dict[str, Any]]:
    """Devices from devices.json merged with LAN IPs from snapshot.json. Keys stripped."""
    raw = _read_json(_dir() / DEVICES_FILE)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else raw.get("devices", []) if isinstance(raw, dict) else []
    ips: dict[str, str] = {}
    versions: dict[str, str] = {}
    snap = _read_json(_dir() / SNAPSHOT_FILE)
    if isinstance(snap, dict):
        for d in snap.get("devices", []) or []:
            if isinstance(d, dict) and d.get("id") and d.get("ip"):
                ips[str(d["id"])] = str(d["ip"])
                if d.get("ver"):
                    versions[str(d["id"])] = str(d["ver"])
    out = []
    for d in items:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        tid = str(d["id"])
        out.append({
            "id": tid,
            "name": str(d.get("name") or "").strip(),
            "mac": norm_mac(d.get("mac")) if d.get("mac") else None,
            "ip": ips.get(tid) or (str(d["ip"]) if d.get("ip") else ""),
            "version": versions.get(tid) or str(d.get("version") or ""),
            "product_name": str(d.get("product_name") or d.get("model") or ""),
            "category": str(d.get("category") or ""),
            "has_key": bool(d.get("key")),
        })
    return out


def local_key(tuya_id: str) -> str | None:
    """Only for the local-control module; never exposed over the API."""
    raw = _read_json(_dir() / DEVICES_FILE)
    items = raw if isinstance(raw, list) else (raw or {}).get("devices", [])
    for d in items or []:
        if isinstance(d, dict) and str(d.get("id")) == tuya_id:
            return d.get("key") or None
    return None


def preview(network_id: str) -> dict[str, Any]:
    """What an import would do, without writing anything."""
    devices = load()
    inv = store.list_devices(network_id)
    by_mac = {d["mac"]: d for d in inv}
    by_ip: dict[str, dict] = {}
    for d in inv:
        for ip in (d.get("last_ip"), d.get("lan_ip")):
            if ip:
                by_ip.setdefault(ip, d)
    matches, unmatched = [], []
    for t in devices:
        rec = by_mac.get(t["mac"]) if t["mac"] else None
        how = "mac" if rec else ""
        if rec is None and t["ip"]:
            rec = by_ip.get(t["ip"])
            how = "ip" if rec else ""
        if rec is None:
            unmatched.append(t)
            continue
        matches.append({
            "tuya": t,
            "mac": rec["mac"],
            "ip": rec.get("last_ip") or rec.get("lan_ip") or "",
            "matched_by": how,
            "current_name": rec.get("name") or "",
            "current_nickname": rec.get("nickname") or "",
            "trusted": bool(rec.get("trusted")),
            "will_rename": bool(t["name"]) and (rec.get("name") or "") != t["name"],
        })
    return {
        "file": available(),
        "dir": str(_dir()),
        "count": len(devices),
        "with_mac": sum(1 for t in devices if t["mac"]),
        "with_ip": sum(1 for t in devices if t["ip"]),
        "with_key": sum(1 for t in devices if t["has_key"]),
        "matches": matches,
        "unmatched": unmatched,
    }


async def apply(network_id: str, *, only_unnamed: bool = True, eero_nickname: bool = True,
                trust: bool = True) -> dict[str, Any]:
    from . import service  # late import: service imports norm_mac from here's sibling
    pv = preview(network_id)
    renamed, skipped, eero_ok, eero_failed = [], [], 0, []
    for m in pv["matches"]:
        t = m["tuya"]
        if not t["name"]:
            skipped.append(m["mac"])
            continue
        if only_unnamed and m["current_name"]:
            skipped.append(m["mac"])
            continue
        rec = store.get_device(m["mac"]) or {}
        notes = rec.get("notes") or ""
        tag = "Tuya %s%s" % (t["product_name"] or "device", " [%s]" % t["category"] if t["category"] else "")
        if tag not in notes:
            notes = (notes + "; " if notes else "") + tag
        store.label_device(m["mac"], name=t["name"], notes=notes, trusted=True if trust else None)
        store.set_tuya(m["mac"], t["id"], t["product_name"])
        renamed.append({"mac": m["mac"], "name": t["name"], "matched_by": m["matched_by"]})
        if eero_nickname and rec.get("device_url") and rec.get("nickname") != t["name"]:
            res = await service.set_eero_nickname(network_id, rec, t["name"])
            if res.get("ok"):
                eero_ok += 1
            else:
                eero_failed.append({"mac": m["mac"], "error": res.get("error", "")})
    store.add_event(network_id, "tuya_import", "",
                    "%d devices named from devices.json (%d matched by MAC, %d by IP, %d unmatched)" % (
                        len(renamed), sum(1 for r in renamed if r["matched_by"] == "mac"),
                        sum(1 for r in renamed if r["matched_by"] == "ip"), len(pv["unmatched"])))
    logger.info("tuya import: %d renamed, %d skipped, %d unmatched", len(renamed), len(skipped), len(pv["unmatched"]))
    return {"renamed": renamed, "skipped": skipped, "unmatched": [t["name"] or t["id"] for t in pv["unmatched"]],
            "eero_nicknames_set": eero_ok, "eero_failed": eero_failed}
