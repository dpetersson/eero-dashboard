"""SQLite persistence for the watch feature.

One file in the data directory: ``watch.sqlite3``. Three tables:

* devices   - one row per MAC ever seen, with trust/label metadata
* sightings - one row per (poll, connected device) with the usage sample
* events    - the alert log shown in the UI and pushed to the webhook

sqlite3 is used synchronously; every call is short and the poller runs at
most every 30 seconds, so a thread offload is not worth the complexity.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DB_PATH = DATA_DIR / "watch.sqlite3"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_db_path_override: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac          TEXT PRIMARY KEY,
    network_id   TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    owner        TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    trusted      INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    last_ip      TEXT NOT NULL DEFAULT '',
    hostname     TEXT NOT NULL DEFAULT '',
    nickname     TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    device_type  TEXT NOT NULL DEFAULT '',
    connection   TEXT NOT NULL DEFAULT '',
    device_url   TEXT NOT NULL DEFAULT '',
    probe_json   TEXT,
    probe_at     TEXT
);
CREATE TABLE IF NOT EXISTS sightings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mac        TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    up         REAL,
    down       REAL
);
CREATE INDEX IF NOT EXISTS sightings_mac_seen ON sightings (mac, seen_at);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    network_id TEXT NOT NULL,
    type       TEXT NOT NULL,
    mac        TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
CREATE TABLE IF NOT EXISTS lan_sightings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mac        TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    hostname   TEXT NOT NULL DEFAULT '',
    vendor     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS lan_sightings_mac_seen ON lan_sightings (mac, seen_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release. Applied with ALTER TABLE when missing.
DEVICE_COLUMNS_V2 = [
    ("lan_last_seen", "TEXT NOT NULL DEFAULT ''"),
    ("lan_ip", "TEXT NOT NULL DEFAULT ''"),
    ("lan_hostname", "TEXT NOT NULL DEFAULT ''"),
    ("lan_source", "TEXT NOT NULL DEFAULT ''"),
    ("oui_vendor", "TEXT NOT NULL DEFAULT ''"),
    ("make_model", "TEXT NOT NULL DEFAULT ''"),
    ("ssid", "TEXT NOT NULL DEFAULT ''"),
    ("segment", "TEXT NOT NULL DEFAULT ''"),
    ("first_source", "TEXT NOT NULL DEFAULT 'eero'"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def use_path(path: Path | None) -> None:
    """Point the store at a different file (tests). Closes any open connection."""
    global _db_path_override, _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        _db_path_override = path


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path_override or DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _migrate(_conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
    for col, decl in DEVICE_COLUMNS_V2:
        if col not in have:
            conn.execute("ALTER TABLE devices ADD COLUMN %s %s" % (col, decl))
    conn.commit()


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _device_row(row: dict[str, Any]) -> dict[str, Any]:
    row["trusted"] = bool(row["trusted"])
    if row.get("probe_json"):
        try:
            row["probe"] = json.loads(row["probe_json"])
        except ValueError:
            row["probe"] = None
    else:
        row["probe"] = None
    row.pop("probe_json", None)
    return row


# --------------------------------------------------------------------------- devices

def get_device(mac: str) -> dict[str, Any] | None:
    with _lock:
        cur = _db().execute("SELECT * FROM devices WHERE mac = ?", (mac,))
        row = cur.fetchone()
    return _device_row(dict(row)) if row else None


def list_devices(network_id: str) -> list[dict[str, Any]]:
    with _lock:
        cur = _db().execute("SELECT * FROM devices WHERE network_id = ? ORDER BY last_seen DESC", (network_id,))
        rows = _rows(cur)
    return [_device_row(r) for r in rows]


def upsert_device(mac: str, network_id: str, seen_at: str, fields: dict[str, Any],
                  first_seen: str | None = None) -> bool:
    """Insert or update the device row. Returns True when the row was created."""
    cols = {k: (v if v is not None else "") for k, v in fields.items()}
    with _lock:
        db = _db()
        existing = db.execute("SELECT mac FROM devices WHERE mac = ?", (mac,)).fetchone()
        if existing is None:
            db.execute(
                """INSERT INTO devices (mac, network_id, first_seen, last_seen, last_ip, hostname, nickname,
                   manufacturer, device_type, connection, device_url, trusted, make_model, ssid, segment,
                   first_source, oui_vendor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mac, network_id, first_seen or seen_at, seen_at, cols.get("ip", ""), cols.get("hostname", ""),
                 cols.get("nickname", ""), cols.get("manufacturer", ""), cols.get("device_type", ""),
                 cols.get("connection", ""), cols.get("device_url", ""), int(cols.get("trusted", 0)),
                 cols.get("make_model", ""), cols.get("ssid", ""), cols.get("segment", ""),
                 cols.get("first_source", "eero"), cols.get("oui_vendor", "")),
            )
            db.commit()
            return True
        db.execute(
            """UPDATE devices SET last_seen = MAX(last_seen, ?), last_ip = CASE WHEN ? != '' THEN ? ELSE last_ip END,
               hostname = CASE WHEN ? != '' THEN ? ELSE hostname END,
               nickname = CASE WHEN ? != '' THEN ? ELSE nickname END,
               manufacturer = CASE WHEN ? != '' THEN ? ELSE manufacturer END,
               device_type = CASE WHEN ? != '' THEN ? ELSE device_type END,
               connection = CASE WHEN ? != '' THEN ? ELSE connection END,
               device_url = CASE WHEN ? != '' THEN ? ELSE device_url END,
               make_model = CASE WHEN ? != '' THEN ? ELSE make_model END,
               ssid = CASE WHEN ? != '' THEN ? ELSE ssid END,
               segment = CASE WHEN ? != '' THEN ? ELSE segment END,
               oui_vendor = CASE WHEN oui_vendor = '' THEN ? ELSE oui_vendor END
               WHERE mac = ?""",
            (seen_at, cols.get("ip", ""), cols.get("ip", ""),
             cols.get("hostname", ""), cols.get("hostname", ""),
             cols.get("nickname", ""), cols.get("nickname", ""),
             cols.get("manufacturer", ""), cols.get("manufacturer", ""),
             cols.get("device_type", ""), cols.get("device_type", ""),
             cols.get("connection", ""), cols.get("connection", ""),
             cols.get("device_url", ""), cols.get("device_url", ""),
             cols.get("make_model", ""), cols.get("make_model", ""),
             cols.get("ssid", ""), cols.get("ssid", ""),
             cols.get("segment", ""), cols.get("segment", ""),
             cols.get("oui_vendor", ""),
             mac),
        )
        db.commit()
        return False


def upsert_lan_device(mac: str, network_id: str, seen_at: str, ip: str, hostname: str,
                      vendor: str, source: str) -> bool:
    """Record a LAN sighting. Creates the row (untrusted, first_source=lan) when unknown."""
    with _lock:
        db = _db()
        existing = db.execute("SELECT mac FROM devices WHERE mac = ?", (mac,)).fetchone()
        if existing is None:
            db.execute(
                """INSERT INTO devices (mac, network_id, first_seen, last_seen, last_ip, trusted,
                   lan_last_seen, lan_ip, lan_hostname, lan_source, oui_vendor, first_source)
                   VALUES (?, ?, ?, '', '', 0, ?, ?, ?, ?, ?, 'lan')""",
                (mac, network_id, seen_at, seen_at, ip, hostname, source, vendor),
            )
            db.commit()
            return True
        db.execute(
            """UPDATE devices SET lan_last_seen = ?, lan_ip = ?, lan_source = ?,
               lan_hostname = CASE WHEN ? != '' THEN ? ELSE lan_hostname END,
               oui_vendor = CASE WHEN ? != '' THEN ? ELSE oui_vendor END
               WHERE mac = ?""",
            (seen_at, ip, source, hostname, hostname, vendor, vendor, mac),
        )
        db.commit()
        return False


def add_lan_sightings(rows: Iterable[tuple[str, str, str, str, str, str]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _lock:
        db = _db()
        db.executemany(
            "INSERT INTO lan_sightings (mac, seen_at, ip, hostname, vendor, source) VALUES (?, ?, ?, ?, ?, ?)", rows)
        db.commit()


def prune_lan_sightings(days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        db = _db()
        cur = db.execute("DELETE FROM lan_sightings WHERE seen_at < ?", (cutoff,))
        db.commit()
        return cur.rowcount


# --------------------------------------------------------------------------- meta

def get_meta(key: str) -> Any:
    with _lock:
        row = _db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except ValueError:
        return row["value"]


def set_meta(key: str, value: Any) -> None:
    with _lock:
        db = _db()
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, json.dumps(value)))
        db.commit()


def label_device(mac: str, *, name: str | None = None, owner: str | None = None,
                 notes: str | None = None, trusted: bool | None = None) -> dict[str, Any] | None:
    sets, vals = [], []
    if name is not None:
        sets.append("name = ?"); vals.append(name)
    if owner is not None:
        sets.append("owner = ?"); vals.append(owner)
    if notes is not None:
        sets.append("notes = ?"); vals.append(notes)
    if trusted is not None:
        sets.append("trusted = ?"); vals.append(int(trusted))
    if sets:
        vals.append(mac)
        with _lock:
            db = _db()
            db.execute("UPDATE devices SET %s WHERE mac = ?" % ", ".join(sets), vals)
            db.commit()
    return get_device(mac)


def set_nickname(mac: str, nickname: str) -> None:
    with _lock:
        db = _db()
        db.execute("UPDATE devices SET nickname = ? WHERE mac = ?", (nickname, mac))
        db.commit()


def mark_eero_node(mac: str, name: str) -> None:
    """A LAN-side interface of an eero node: trusted, typed, named, never 'new'."""
    with _lock:
        db = _db()
        db.execute("""UPDATE devices SET trusted = 1, device_type = 'eero_node', manufacturer = 'eero',
                      name = CASE WHEN name = '' THEN ? ELSE name END WHERE mac = ?""", (name, mac))
        db.commit()


def trust_all(network_id: str) -> int:
    with _lock:
        db = _db()
        cur = db.execute("UPDATE devices SET trusted = 1 WHERE network_id = ? AND trusted = 0", (network_id,))
        db.commit()
        return cur.rowcount


def set_probe(mac: str, result: dict[str, Any]) -> None:
    with _lock:
        db = _db()
        db.execute("UPDATE devices SET probe_json = ?, probe_at = ? WHERE mac = ?",
                   (json.dumps(result), now_iso(), mac))
        db.commit()


def delete_device(mac: str) -> None:
    with _lock:
        db = _db()
        db.execute("DELETE FROM devices WHERE mac = ?", (mac,))
        db.execute("DELETE FROM sightings WHERE mac = ?", (mac,))
        db.execute("DELETE FROM lan_sightings WHERE mac = ?", (mac,))
        db.commit()


# --------------------------------------------------------------------------- sightings

def add_sightings(rows: Iterable[tuple[str, str, str, float | None, float | None]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _lock:
        db = _db()
        db.executemany("INSERT INTO sightings (mac, seen_at, ip, up, down) VALUES (?, ?, ?, ?, ?)", rows)
        db.commit()


def samples(mac: str, hours: int = 24) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _lock:
        cur = _db().execute(
            "SELECT seen_at, ip, up, down FROM sightings WHERE mac = ? AND seen_at >= ? ORDER BY seen_at",
            (mac, since),
        )
        return _rows(cur)


def upload_stats(network_id: str, hours: int = 24) -> dict[str, dict[str, float]]:
    """Per-MAC upload statistics over the window: sample count, uploading count, median up."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _lock:
        cur = _db().execute(
            """SELECT s.mac AS mac, s.up AS up FROM sightings s
               JOIN devices d ON d.mac = s.mac
               WHERE d.network_id = ? AND s.seen_at >= ? AND s.up IS NOT NULL
               ORDER BY s.mac, s.up""",
            (network_id, since),
        )
        rows = cur.fetchall()
    by_mac: dict[str, list[float]] = {}
    for r in rows:
        by_mac.setdefault(r["mac"], []).append(float(r["up"]))
    out = {}
    for mac, ups in by_mac.items():
        ups.sort()
        n = len(ups)
        median = ups[n // 2] if n % 2 else (ups[n // 2 - 1] + ups[n // 2]) / 2
        out[mac] = {"samples": n, "median_up": median, "max_up": ups[-1], "ups": ups}
    return out


def prune_sightings(days: int = 30) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        db = _db()
        cur = db.execute("DELETE FROM sightings WHERE seen_at < ?", (cutoff,))
        db.commit()
        return cur.rowcount


# --------------------------------------------------------------------------- events

def add_event(network_id: str, type_: str, mac: str = "", detail: str = "") -> dict[str, Any]:
    at = now_iso()
    with _lock:
        db = _db()
        cur = db.execute("INSERT INTO events (at, network_id, type, mac, detail) VALUES (?, ?, ?, ?, ?)",
                         (at, network_id, type_, mac, detail))
        db.commit()
        event_id = cur.lastrowid
    return {"id": event_id, "at": at, "network_id": network_id, "type": type_, "mac": mac, "detail": detail}


def list_events(network_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        cur = _db().execute(
            "SELECT * FROM events WHERE network_id = ? ORDER BY id DESC LIMIT ?", (network_id, limit))
        return _rows(cur)


def last_event_of(network_id: str, type_: str, mac: str) -> dict[str, Any] | None:
    with _lock:
        cur = _db().execute(
            "SELECT * FROM events WHERE network_id = ? AND type = ? AND mac = ? ORDER BY id DESC LIMIT 1",
            (network_id, type_, mac))
        row = cur.fetchone()
    return dict(row) if row else None


def prune_events(days: int = 90) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        db = _db()
        cur = db.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        db.commit()
        return cur.rowcount
