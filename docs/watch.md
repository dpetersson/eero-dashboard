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
