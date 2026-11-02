# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Watch** tab (`features/watch`): SQLite device inventory with a trusted baseline, new-device and returned-device events, a 24h steady-upload rule, webhook alerts (Slack or ntfy), an optional nmap camera-port probe, and trust/label/block/forget actions. See `docs/watch.md`.
- LAN cross-reference: built-in ARP sweep (`features/watch/lanscan.py`, `WATCH_LAN_SCAN`), ingest endpoint for sweeps pushed by `netscan.py`, OUI vendor fill-in, subnet-aware comparison of eero's list against what answers on the wire, `new_device_lan` events.
- `core/lan_guard.py`: rejects `/api/*` requests from non-private source addresses (`WATCH_LAN_ONLY`, `WATCH_ALLOWED_CIDRS`).
- Demo mode exercises the Watch tab against the synthetic device list; `/watch/*` mutations bypass the demo no-op short-circuit.

## [1.0.0] - 2026-05-23

First tagged release. Promotes the project from the unstable `0.y.z` line — the REST surface (`/api/*`) and container env vars are now committed to SemVer compatibility going forward.

### Fixed
- Sidebar not scrolling when content exceeds viewport height; wheel/trackpad events over the sidebar no longer hijack the main content panel ([#46](https://github.com/xrvk/eero-dashboard/issues/46), [#51](https://github.com/xrvk/eero-dashboard/pull/51)).
