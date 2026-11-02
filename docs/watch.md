# Watch: inventory baseline and new-device alerts

The Watch tab turns the device list eero already reports into a security
inventory. It records every MAC the network has ever seen, flags anything
that was not in the baseline, samples per-device upload every poll, and
raises an event when a device uploads around the clock.

It exists because the eero app answers "what is connected right now" but not
"what connected for the first time last Tuesday at 3 am" or "which device has
been uploading non-stop for a week".

## How it works

1. A background poller fetches the device list for every network on a fixed
   interval (`WATCH_POLL_SECONDS`, default 5 minutes). It reuses the
   dashboard's normal cached fetch, so it adds no extra load on eero's API.
2. The first poll for a network records every connected device as the
   trusted baseline.
3. Later polls insert any unknown MAC as **untrusted** and write a
   `new_device` event. Known devices get `last_seen` and `last_ip` updated.
4. Every poll stores one usage sample per connected device. The steady-upload
   rule looks at the last 24 hours: a device uploading in at least
   `WATCH_UPLOAD_RATIO` (default 90%) of its samples, with at least
   `WATCH_UPLOAD_SAMPLES` samples, gets a `steady_upload` event at most once
   per day. A note beginning with `ok:` on a trusted device silences the rule
   for that device, for things like a NAS backup that uploads all night.
5. `new_device` and `steady_upload` events are also pushed to
   `WATCH_WEBHOOK` if set. The URL may be a Slack incoming webhook or an
   ntfy topic (`https://ntfy.sh/<topic>`).

Everything lives in `data/watch.sqlite3` inside the data volume. Nothing is
written back to eero unless you press **Block**, which calls the existing
block endpoint.

## The tab

- **Untrusted devices**: everything not yet labelled. Trust, name, probe,
  block, or forget each one. Forgetting a device makes it show as new again
  if it returns.
- **New in the last N days**: recent joiners regardless of trust
  (`WATCH_NEW_DAYS`).
- **Top uploaders (24h)**: ranked by the fraction of samples with upload.
  Rows at 90% or more are highlighted.
- **Recent events**: the alert log.
- **Poll now** forces a poll. **Trust all** marks the whole inventory trusted,
  useful right after first deployment once you have reviewed the list.

## LAN cross-reference

eero's device list is eero's opinion. The Watch tab keeps a second, independent
source: an ARP sweep of the subnet, either run by the dashboard itself or pushed
to it by the standalone `netscan.py`. The sweep pings every address (which
forces an ARP exchange), reads the ARP table, looks vendors up in the IEEE OUI
table, and resolves names through reverse DNS and mDNS. No root, no extra
tools.

What you get from the pairing:

- **Devices on the wire that eero does not list.** Static-IP hosts, a second
  router, anything behind a non-eero access point, or something eero cannot
  see. They are created untrusted with `first_source=lan` and raise a
  `new_device_lan` event (also sent to the webhook).
- **Vendor names eero leaves blank.** About a quarter of devices arrive with
  no manufacturer. The OUI prefix fills most of them.
- **A scanner that keeps working when eero's cloud is unreachable.**

Comparison is subnet-aware: eero devices on other VLANs or the guest network
are not counted as "silent" when the sweep covered only the main subnet.

Three ways to feed it:

1. **Button.** *LAN scan* in the tab sweeps the subnet the backend is on.
   Needs `ping` and a default route; the Docker image includes both. Behind a
   Docker bridge the container only sees its own bridge network, so use
   `network_mode: host`.
2. **Schedule.** `WATCH_LAN_SCAN=true` sweeps every `WATCH_LAN_SCAN_SECONDS`
   (default 15 minutes). The first sweep records the gateway MAC; later sweeps
   are skipped when the gateway differs, so a laptop running the backend at a
   coffee shop does not scan the wrong network.
3. **Push from netscan.py.** On any machine on the LAN:
   `netscan.py push --url http://<dashboard>:8420`, or `scan --push URL` from
   its LaunchAgent. It POSTs to `/api/watch/lan-scan`, which attaches the
   sweep to the network the eero poller last saw.

The OUI table: the image ships nmap's prefix file. For the full IEEE table
press *Download it* in the tab or `POST /api/watch/oui/update`; it is saved in
the data volume.

## Probe

The probe button runs an unprivileged nmap TCP scan of the device against
the ports IP cameras and NVRs usually serve (80, 443, 554, 1935, 8000, 8080,
8443, 8554, 9000, 34567, 37777) and reports open ones. It needs `nmap` on the
dashboard host and the dashboard must be on the same LAN as the device.

The stock image does not include nmap. Add it with:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends nmap && rm -rf /var/lib/apt/lists/*
```

A clean probe does not clear a device. Cloud cameras from Wyze, Ring, Nest
and most Tuya brands expose no inbound ports at all. The upload rule and the
eero app's data usage are the signals for those.

## LAN-only guard

The dashboard holds a full-control eero session, so it must never be
reachable from outside the home. `core/lan_guard.py` rejects any `/api/*`
request whose source address is not loopback or RFC 1918 private
(`WATCH_LAN_ONLY=true`, the default). Set `WATCH_ALLOWED_CIDRS` to the
actual home subnet to tighten it, for example `192.168.7.0/24`.

The guard only sees the real client address when the container runs with
host networking or the backend runs directly on the host. Behind a Docker
bridge every request arrives from the bridge gateway and passes trivially.
On a Synology NAS use:

```yaml
services:
  eero-dashboard:
    network_mode: host
    environment:
      - WATCH_ALLOWED_CIDRS=192.168.7.0/24
      - WATCH_WEBHOOK=https://ntfy.sh/your-private-topic
```

Host networking also lets the probe reach devices by their LAN address. Do
not port-forward 8420 and do not put the dashboard behind a tunnel. Remote
management stays with the eero app.

## Environment variables

| Variable | Default | Description |
|----|----|----|
| `WATCH_ENABLED` | `true` | Run the background poller |
| `WATCH_POLL_SECONDS` | `300` | Seconds between polls (minimum 30) |
| `WATCH_WEBHOOK` | unset | Slack incoming webhook or ntfy topic URL |
| `WATCH_NEW_DAYS` | `7` | Window for the "new" section |
| `WATCH_ABSENT_DAYS` | `14` | Log a `returned` event after this many days away (0 off) |
| `WATCH_UPLOAD_MIN` | `0` | Upload sample must exceed this to count as uploading |
| `WATCH_UPLOAD_RATIO` | `0.9` | Fraction of 24h samples uploading that triggers the rule |
| `WATCH_UPLOAD_SAMPLES` | `12` | Minimum samples before the rule applies |
| `WATCH_LAN_ONLY` | `true` | Reject API requests from non-private addresses |
| `WATCH_ALLOWED_CIDRS` | unset | Comma list of CIDRs allowed when set |
| `WATCH_LAN_SCAN` | `false` | Sweep the local subnet on a schedule from this host |
| `WATCH_LAN_SCAN_SECONDS` | `900` | Seconds between built-in sweeps (minimum 60) |
| `WATCH_LAN_SCAN_SUBNET` | auto | CIDR to sweep instead of the detected one |
| `WATCH_NETWORK_ID` | unset | eero network id for LAN scans before the first eero poll |

## API

All under `/api/networks/{network_id}/watch`:

- `GET /summary` counts, untrusted, new, uploaders, events, config
- `GET /devices` full inventory
- `GET /devices/{mac}` one device with 24h samples
- `PATCH /devices/{mac}` body `{name, owner, notes, trusted}` (all optional)
- `DELETE /devices/{mac}` forget
- `POST /devices/{mac}/probe` nmap probe (501 when nmap is absent)
- `GET /events?limit=100`
- `POST /poll` force a poll of every network
- `POST /baseline` mark every known device trusted
- `GET /lan-scan` last sweep and whether this host can sweep
- `POST /lan-scan/run` sweep from this host and merge
- `POST /lan-scan` ingest a sweep pushed by netscan.py
- `POST /api/watch/lan-scan` same, network resolved server-side
- `POST /api/watch/oui/update` download the IEEE OUI table

These endpoints read the local inventory, never eero, so they keep working
in demo mode and the demo middleware lets their mutations through.

## Limits

- Devices that record to a microSD card or use a cellular modem never join
  the network. Only a physical sweep finds those.
- Phones with rotating private Wi-Fi addresses appear as new devices
  periodically. Set them to a fixed address for the home network.
- Vendor from the MAC prefix is a hint. Espressif, Realtek and Tuya cover
  smart plugs and cameras alike.
- Usage values are whatever eero reports. The rule compares a device against
  its own history, not against an absolute bandwidth.
