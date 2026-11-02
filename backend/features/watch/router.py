from fastapi import APIRouter, HTTPException

from . import poller, schemas, service, store

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


def _device_or_404(mac: str) -> dict:
    norm = service.norm_mac(mac)
    rec = store.get_device(norm) if norm else None
    if rec is None:
        raise HTTPException(status_code=404, detail="Device not in watch inventory")
    return rec
