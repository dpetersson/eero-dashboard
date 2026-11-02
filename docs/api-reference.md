# API Reference (Backend)

Base path: `/api`

**Sections:**

- [Health](#health)
- [Auth](#auth)
- [Networks](#networks)
- [Devices](#devices)
- [Profiles](#profiles)
- [Network Ops](#network-ops)
- [Speed Test](#speed-test)
- [Security](#security)
- [Port Forwards](#port-forwards)
- [DHCP Reservations](#dhcp-reservations)
- [Guest Network](#guest-network)
- [Network Name & Password](#network-name-password)
- [Node Management](#node-management)
- [SQM / QoS](#sqm-qos)
- [Firmware Updates](#firmware-updates)
- [Network Reboot](#network-reboot)
- [Thread & Routing](#thread-routing)
- [Blacklist](#blacklist)
- [Watch](#watch)

## Health

- `GET /health`

## Auth

- `GET /auth/status`
- `POST /auth/login`
- `POST /auth/verify`
- `POST /auth/logout`

## Networks

- `GET /networks`
- `GET /networks/{network_id}`
- `GET /networks/{network_id}/eeros`
- `GET /networks/{network_id}/settings`

## Devices

- `GET /networks/{network_id}/devices`
- `GET /networks/{network_id}/devices/{device_id}`
- `POST /networks/{network_id}/devices/{device_id}/pause`
- `POST /networks/{network_id}/devices/{device_id}/block`
- `POST /networks/{network_id}/devices/{device_id}/rename` *(compat)*
- `POST /networks/{network_id}/devices/{device_id}/nickname`
- `GET /networks/{network_id}/devices/{device_id}/priority`
- `POST /networks/{network_id}/devices/{device_id}/priority`

## Profiles

- `GET /networks/{network_id}/profiles`
- `POST /networks/{network_id}/profiles`
- `GET /networks/{network_id}/profiles/{profile_id}`
- `POST /networks/{network_id}/profiles/{profile_id}/pause`
- `GET /networks/{network_id}/profiles/{profile_id}/blocked-apps`
- `POST /networks/{network_id}/profiles/{profile_id}/blocked-apps`
- `POST /networks/{network_id}/profiles/{profile_id}/bedtime`
- `GET /networks/{network_id}/profiles/{profile_id}/schedule`
- `POST /networks/{network_id}/profiles/{profile_id}/schedule/set`
- `DELETE /networks/{network_id}/profiles/{profile_id}/schedule`
- `PUT /networks/{network_id}/profiles/{profile_id}/devices`
- `PUT /networks/{network_id}/profiles/{profile_id}/rename`
- `DELETE /networks/{network_id}/profiles/{profile_id}`

## Network Ops

- `POST /prefetch/{network_id}`
- `GET /networks/{network_id}/dns`
- `POST /networks/{network_id}/dns/mode`
- `POST /networks/{network_id}/dns/caching`
- `GET /networks/{network_id}/activity`
- `GET /networks/{network_id}/activity/history`
- `GET /networks/{network_id}/activity/clients`
- `GET /networks/{network_id}/activity/categories`
- `GET /networks/{network_id}/diagnostics`
- `POST /networks/{network_id}/diagnostics`

## Speed Test

- `POST /networks/{network_id}/speed-test`
- `GET /networks/{network_id}/speed-history`

## Security

- `GET /networks/{network_id}/security`
- `PATCH /networks/{network_id}/security`

## Port Forwards

- `GET /networks/{network_id}/forwards`
- `POST /networks/{network_id}/forwards`
- `DELETE /networks/{network_id}/forwards/{forward_id}`

## DHCP Reservations

- `GET /networks/{network_id}/reservations`
- `POST /networks/{network_id}/reservations`
- `DELETE /networks/{network_id}/reservations/{reservation_id}`

## Guest Network

- `POST /networks/{network_id}/guest`

## Network Name & Password

- `GET /networks/{network_id}/password`
- `POST /networks/{network_id}/name`

## Node Management

- `GET /networks/{network_id}/eeros/{eero_id}`
- `POST /networks/{network_id}/eeros/{eero_id}/reboot`
- `GET /networks/{network_id}/eeros/{eero_id}/led`
- `POST /networks/{network_id}/eeros/{eero_id}/led`
- `POST /networks/{network_id}/eeros/{eero_id}/led/brightness`
- `GET /networks/{network_id}/eeros/{eero_id}/nightlight`
- `POST /networks/{network_id}/eeros/{eero_id}/nightlight`

## SQM / QoS

- `GET /networks/{network_id}/sqm`
- `POST /networks/{network_id}/sqm`
- `POST /networks/{network_id}/sqm/configure`
- `POST /networks/{network_id}/sqm/auto`

## Firmware Updates

- `GET /networks/{network_id}/updates`

## Network Reboot

- `POST /networks/{network_id}/reboot`

## Thread & Routing

- `GET /networks/{network_id}/thread`
- `GET /networks/{network_id}/routing`

## Blacklist

- `GET /networks/{network_id}/blacklist`
- `POST /networks/{network_id}/blacklist/{device_id}`
- `DELETE /networks/{network_id}/blacklist/{device_id}`

For endpoint source-of-truth, see:

- `backend/main.py`
- `backend/features/**/router.py`

## Watch

Local inventory, never proxied to eero. See [watch.md](./watch.md).

- `GET /networks/{network_id}/watch/summary`
- `GET /networks/{network_id}/watch/devices`
- `GET /networks/{network_id}/watch/devices/{mac}`
- `PATCH /networks/{network_id}/watch/devices/{mac}`
- `DELETE /networks/{network_id}/watch/devices/{mac}`
- `POST /networks/{network_id}/watch/devices/{mac}/probe`
- `GET /networks/{network_id}/watch/events`
- `POST /networks/{network_id}/watch/poll`
- `POST /networks/{network_id}/watch/baseline`
- `GET /networks/{network_id}/watch/lan-scan`
- `POST /networks/{network_id}/watch/lan-scan/run`
- `POST /networks/{network_id}/watch/lan-scan`
- `POST /watch/lan-scan`
- `POST /watch/oui/update`
- `GET /networks/{network_id}/watch/identify`
- `POST /networks/{network_id}/watch/identify/start`
- `POST /networks/{network_id}/watch/identify/stop`
- `DELETE /networks/{network_id}/watch/identify/{mac}`
