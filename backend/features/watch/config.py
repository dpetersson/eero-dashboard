"""Watch feature configuration (environment variables).

All settings are read lazily so tests can override the environment.

WATCH_ENABLED        default true   - run the background poller
WATCH_POLL_SECONDS   default 300    - seconds between device-list polls
WATCH_WEBHOOK        default unset  - Slack incoming webhook or https://ntfy.sh/<topic>
WATCH_NEW_DAYS       default 7      - "new device" window shown in the UI
WATCH_ABSENT_DAYS    default 14     - report a device returning after this many days away
WATCH_UPLOAD_MIN     default 0      - minimum upload sample value to count as "uploading"
WATCH_UPLOAD_RATIO   default 0.9    - fraction of 24h samples uploading to flag steady upload
WATCH_UPLOAD_SAMPLES default 12     - minimum samples in 24h before the rule applies
WATCH_LAN_ONLY       default true   - reject API requests from non-private addresses
WATCH_ALLOWED_CIDRS  default unset  - comma list of CIDRs; when set, only these may use the API
WATCH_LAN_SCAN       default false  - run the built-in ARP sweep from this host (needs host networking)
WATCH_LAN_SCAN_SECONDS default 900  - seconds between built-in sweeps
WATCH_LAN_SCAN_SUBNET default auto  - CIDR to sweep instead of the detected one
WATCH_NETWORK_ID     default unset  - eero network id to attach LAN scans to (auto after first eero poll)
"""

from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return _bool("WATCH_ENABLED", True)


def poll_seconds() -> int:
    return max(30, _int("WATCH_POLL_SECONDS", 300))


def webhook() -> str | None:
    return os.environ.get("WATCH_WEBHOOK") or None


def new_days() -> int:
    return max(1, _int("WATCH_NEW_DAYS", 7))


def absent_days() -> int:
    return max(0, _int("WATCH_ABSENT_DAYS", 14))


def upload_min() -> float:
    return _float("WATCH_UPLOAD_MIN", 0.0)


def upload_ratio() -> float:
    return min(1.0, max(0.0, _float("WATCH_UPLOAD_RATIO", 0.9)))


def upload_samples() -> int:
    return max(2, _int("WATCH_UPLOAD_SAMPLES", 12))


def lan_only() -> bool:
    return _bool("WATCH_LAN_ONLY", True)


def allowed_cidrs() -> list[str]:
    raw = os.environ.get("WATCH_ALLOWED_CIDRS", "")
    return [c.strip() for c in raw.split(",") if c.strip()]


def lan_scan() -> bool:
    return _bool("WATCH_LAN_SCAN", False)


def lan_scan_seconds() -> int:
    return max(60, _int("WATCH_LAN_SCAN_SECONDS", 900))


def lan_scan_subnet() -> str | None:
    return os.environ.get("WATCH_LAN_SCAN_SUBNET") or None


def network_id() -> str | None:
    return os.environ.get("WATCH_NETWORK_ID") or None
