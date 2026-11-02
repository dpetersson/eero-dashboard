from fastapi import APIRouter, HTTPException

from . import lanscan, poller, schemas, service, store

router = APIRouter(prefix="/api", tags=["watch"])


@router.get("/networks/{network_id}/watch/summary")
async def watch_summary(network_id: str):
    return service.summary(network_id)


@router.get("/networks/{network_id}/watch/devices")
async def watch_devices(network_id: str):
    return {"devices": store.list_devices(network_id)}


@router.get("/networks/{network_id}/watch/events")
async def watch_events(network_id: str, limit: int = 100):
    return {"events": store.list_events(network_id, limit=min(max(limit, 1), 1000))}


@router.post("/networks/{network_id}/watch/poll")
async def watch_poll(network_id: str):
    results = await poller.poll_all()
    for r in results:
        r.pop("events", None)
    return {"results": results}


@router.post("/networks/{network_id}/watch/baseline")
async def watch_baseline(network_id: str):
    """Mark every device currently in the inventory as trusted."""
    changed = store.trust_all(network_id)
    store.add_event(network_id, "baseline", "", "%d devices marked trusted" % changed)
    return {"trusted": changed}


@router.get("/networks/{network_id}/watch/devices/{mac}")
async def watch_device(network_id: str, mac: str):
    rec = _device_or_404(mac)
    rec["samples"] = store.samples(rec["mac"], hours=24)
    return rec


@router.patch("/networks/{network_id}/watch/devices/{mac}")
async def watch_label(network_id: str, mac: str, req: schemas.LabelRequest):
    rec = _device_or_404(mac)
    updated = store.label_device(rec["mac"], name=req.name, owner=req.owner, notes=req.notes, trusted=req.trusted)
    if req.trusted is not None and req.trusted != rec["trusted"]:
        store.add_event(network_id, "trusted" if req.trusted else "untrusted", rec["mac"],
                        service.display_name(updated or rec))
    return updated


@router.delete("/networks/{network_id}/watch/devices/{mac}")
async def watch_forget(network_id: str, mac: str):
    rec = _device_or_404(mac)
    store.delete_device(rec["mac"])
    return {"deleted": rec["mac"]}


@router.post("/networks/{network_id}/watch/devices/{mac}/probe")
async def watch_probe(network_id: str, mac: str):
    rec = _device_or_404(mac)
    result = await service.probe_device(rec["mac"])
    if not result.get("ok") and "not installed" in str(result.get("error", "")):
        raise HTTPException(status_code=501, detail=result["error"])
    store.add_event(network_id, "probe", rec["mac"], result.get("verdict") or result.get("error", ""))
    return result


# --------------------------------------------------------------------------- LAN scans

@router.get("/networks/{network_id}/watch/lan-scan")
async def lan_scan_status(network_id: str):
    return {
        "available": lanscan.available(),
        "builtin_enabled": service.config.lan_scan(),
        "oui_loaded": lanscan.oui_available(),
        "last_scan": store.get_meta("lan_scan:%s" % network_id),
        "home_gateway_mac": store.get_meta("home_gateway_mac"),
    }


@router.post("/networks/{network_id}/watch/lan-scan/run")
async def lan_scan_run(network_id: str):
    """Sweep the subnet this backend sits on and merge it. Takes 10-30 s on a /24."""
    if not lanscan.available():
        raise HTTPException(status_code=501, detail="This host cannot sweep: no ping binary or no default route")
    result = await poller.lan_scan_once(network_id)
    if result is None:
        raise HTTPException(status_code=409, detail="No network id known yet")
    result.pop("events", None)
    return result


@router.post("/networks/{network_id}/watch/lan-scan")
async def lan_scan_ingest(network_id: str, scan: schemas.LanScanPush):
    """Accept a sweep pushed by netscan.py from another machine on the LAN."""
    result = service.ingest_lan_scan(network_id, scan.model_dump(), scan.source)
    result.pop("events", None)
    return result


@router.post("/watch/lan-scan")
async def lan_scan_ingest_default(scan: schemas.LanScanPush):
    """Same as above without a network id; uses the network the eero poller last saw."""
    network_id = service.default_network_id()
    if not network_id:
        raise HTTPException(status_code=409, detail="No network id known yet; log in to the dashboard first or set WATCH_NETWORK_ID")
    result = service.ingest_lan_scan(network_id, scan.model_dump(), scan.source)
    result.pop("events", None)
    result["network_id"] = network_id
    return result


@router.post("/watch/oui/update")
async def oui_update():
    try:
        size = lanscan.update_oui()
    except Exception as e:
        raise HTTPException(status_code=502, detail="OUI download failed: %s" % e)
    return {"bytes": size}


def _device_or_404(mac: str) -> dict:
    norm = service.norm_mac(mac)
    rec = store.get_device(norm) if norm else None
    if rec is None:
        raise HTTPException(status_code=404, detail="Device not in watch inventory")
    return rec
