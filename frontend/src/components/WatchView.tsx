import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { ShieldAlert, ShieldCheck, RefreshCw, Radar, Ban, Pencil, Trash2, Network, Crosshair, Square, FileInput } from 'lucide-react';

import * as api from '../api';
import { useFetch } from '../hooks/useFetch';
import type { IdentifyStatus, TuyaImportResult, TuyaPreview, WatchDevice, WatchEvent, WatchSummary } from '../api/watch';

interface WatchViewProps {
  networkId: string;
}

const EVENT_LABEL: Record<string, string> = {
  baseline: 'Baseline',
  new_device: 'New device',
  returned: 'Returned',
  steady_upload: 'Steady upload',
  trusted: 'Trusted',
  untrusted: 'Untrusted',
  probe: 'Probe',
  nickname: 'eero nickname',
  new_device_lan: 'On wire, not in eero',
  tuya_import: 'Tuya import',
};

function ago(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return '';
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function WatchView({ networkId }: WatchViewProps) {
  const summaryKey = `/networks/${networkId}/watch/summary`;
  const { data, loading, error, refetch } = useFetch<WatchSummary>(
    () => api.getWatchSummary(networkId),
    [networkId],
    { retries: 1 },
    summaryKey,
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [editing, setEditing] = useState<WatchDevice | null>(null);
  const [probeResult, setProbeResult] = useState<{ mac: string; result: api.WatchProbe } | null>(null);
  const [identify, setIdentify] = useState<IdentifyStatus | null>(null);
  const [identifyNames, setIdentifyNames] = useState<Record<string, string>>({});
  const [tuya, setTuya] = useState<TuyaPreview | null>(null);
  const [tuyaResult, setTuyaResult] = useState<TuyaImportResult | null>(null);
  const [tuyaOnlyUnnamed, setTuyaOnlyUnnamed] = useState(true);

  const run = useCallback(async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setActionError(null);
    try {
      await fn();
      refetch();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setBusy(null);
    }
  }, [refetch]);

  const trust = (d: WatchDevice, trusted: boolean) =>
    run(`trust:${d.mac}`, () => api.labelWatchDevice(networkId, d.mac, { trusted }));
  const block = (d: WatchDevice) => {
    const id = d.device_url ? d.device_url.replace(/\/$/, '').split('/').pop() || d.mac : d.mac;
    return run(`block:${d.mac}`, () => api.blockDevice(networkId, id, true));
  };
  const probe = (d: WatchDevice) =>
    run(`probe:${d.mac}`, async () => {
      const result = await api.probeWatchDevice(networkId, d.mac);
      setProbeResult({ mac: d.mac, result });
    });
  const forget = (d: WatchDevice) => run(`forget:${d.mac}`, () => api.forgetWatchDevice(networkId, d.mac));
  const pollNow = () => run('poll', () => api.runWatchPoll(networkId));
  const trustAll = () => run('trust-all', () => api.trustAllWatch(networkId));
  const lanScan = () => run('lan-scan', async () => {
    const r = await api.runLanScan(networkId);
    if (r.skipped) throw new Error(`LAN scan skipped: ${r.reason}`);
  });
  const fetchOui = () => run('oui', () => api.updateOui());

  const saveLabel = useCallback(async (mac: string, body: { name?: string; owner?: string; notes?: string; trusted?: boolean; eero_nickname?: boolean }) => {
    const res = await api.labelWatchDevice(networkId, mac, body);
    if (body.eero_nickname && res.eero && !res.eero.ok) {
      throw new Error(`Saved locally, but eero nickname was not set: ${res.eero.error}`);
    }
    return res;
  }, [networkId]);

  // Identify mode: poll while active.
  useEffect(() => {
    if (!identify?.active) return;
    const id = window.setInterval(() => {
      api.getIdentifyStatus(networkId).then(setIdentify).catch(() => {});
    }, Math.max(1000, (identify.interval_s || 2) * 1000));
    return () => window.clearInterval(id);
  }, [identify?.active, identify?.interval_s, networkId]);
  useEffect(() => {
    api.getIdentifyStatus(networkId).then(setIdentify).catch(() => {});
  }, [networkId]);
  const tuyaFile = !!data?.config.tuya_file;
  useEffect(() => {
    if (!tuyaFile) return;
    let cancelled = false;
    api.getTuyaPreview(networkId).then((p) => { if (!cancelled) setTuya(p); }).catch(() => {});
    return () => { cancelled = true; };
  }, [networkId, tuyaFile]);
  const importTuya = () => run('tuya', async () => {
    const res = await api.runTuyaImport(networkId, { only_unnamed: tuyaOnlyUnnamed, eero_nickname: true, trust: true });
    setTuyaResult(res);
    setTuya(await api.getTuyaPreview(networkId));
  });
  const startIdentify = () => run('identify', async () => setIdentify(await api.startIdentify(networkId)));
  const stopIdentify = () => run('identify', async () => setIdentify(await api.stopIdentify(networkId)));
  const nameIdentified = (mac: string) => run(`identify:${mac}`, async () => {
    const name = (identifyNames[mac] || '').trim();
    if (!name) throw new Error('Enter a name first');
    await saveLabel(mac, { name, trusted: true, eero_nickname: true });
    setIdentify(await api.removeIdentifyTarget(networkId, mac));
  });

  if (loading && !data) return <div className="card loading-card"><div className="spinner" /> Loading watch data…</div>;
  if (error && !data) return <div className="card error-card">Watch data unavailable: {error}</div>;
  if (!data) return null;

  const { counts, config, cross } = data;

  return (
    <div className="watch-view">
      <div className="watch-stats">
        <Stat label="Online" value={counts.online} sub={`${counts.total} known`} />
        <Stat label="Untrusted" value={counts.untrusted} sub={`${counts.untrusted_online} online now`} tone={counts.untrusted ? 'red' : 'green'} />
        <Stat label={`New (${config.new_days}d)`} value={counts.new} tone={counts.new ? 'yellow' : undefined} />
        <Stat label="Steady uploaders" value={data.uploaders.filter((d) => (d.upload?.ratio ?? 0) >= 0.9).length} tone="muted" />
      </div>

      <div className="watch-toolbar">
        <span className="watch-config">
          Polls every {Math.round(config.poll_seconds / 60)} min
          {' · '}{config.webhook_configured ? 'webhook on' : 'no webhook'}
          {' · '}{config.probe_available ? 'nmap available' : 'nmap not installed'}
          {' · '}{config.lan_only ? (config.allowed_cidrs.length ? `LAN only: ${config.allowed_cidrs.join(', ')}` : 'LAN only') : 'LAN guard off'}
        </span>
        <div className="watch-toolbar-actions">
          <button className="btn-action btn-sm" onClick={pollNow} disabled={busy === 'poll'}>
            <RefreshCw size={14} /> Poll now
          </button>
          {cross.available && (
            <button className="btn-action btn-sm" onClick={lanScan} disabled={busy === 'lan-scan'}
              title="Ping-sweep the subnet this dashboard host is on and merge the ARP table (10-30 s)">
              <Network size={14} /> {busy === 'lan-scan' ? 'Scanning…' : 'LAN scan'}
            </button>
          )}
          {counts.untrusted > 0 && (
            <button className="btn-action btn-sm" onClick={trustAll} disabled={busy === 'trust-all'} title="Mark every known device trusted">
              <ShieldCheck size={14} /> Trust all
            </button>
          )}
        </div>
      </div>

      {actionError && <div className="card error-card">{actionError}</div>}

      {tuyaFile && tuya && tuya.file && (
        <div className="watch-section">
          <div className="section-header">
            <h2><FileInput size={16} />Tuya names from devices.json <span className={`badge ${tuya.matches.length ? '' : 'muted'}`}>{tuya.matches.length}</span></h2>
            <div className="watch-toolbar-actions">
              <label className="watch-check watch-inline-check">
                <input type="checkbox" checked={tuyaOnlyUnnamed} onChange={(e) => setTuyaOnlyUnnamed(e.target.checked)} /> Only unnamed
              </label>
              <button className="btn-primary btn-sm" onClick={importTuya}
                disabled={busy === 'tuya' || !tuya.matches.some((m) => !tuyaOnlyUnnamed || !m.current_name)}>
                Import {tuya.matches.filter((m) => !tuyaOnlyUnnamed || !m.current_name).length} names
              </button>
            </div>
          </div>
          <div className="card">
            <p className="watch-hint">
              {tuya.count} devices in {tuya.dir}: {tuya.with_mac} with a MAC, {tuya.with_ip} with a LAN IP, {tuya.with_key} with a local key.
              {' '}{tuya.matches.length} match the inventory ({tuya.matches.filter((m) => m.matched_by === 'mac').length} by MAC,
              {' '}{tuya.matches.filter((m) => m.matched_by === 'ip').length} by IP), {tuya.unmatched.length} do not.
              Importing names and trusts each match and writes the name to eero as the nickname. Local keys stay in the file.
            </p>
            {tuyaResult && (
              <p className="watch-hint">
                Last import: {tuyaResult.renamed.length} named, {tuyaResult.skipped.length} skipped, {tuyaResult.unmatched.length} unmatched,
                {' '}{tuyaResult.eero_nicknames_set} eero nicknames set{tuyaResult.eero_failed.length ? `, ${tuyaResult.eero_failed.length} eero writes failed (${tuyaResult.eero_failed[0].error})` : ''}.
              </p>
            )}
            <table className="watch-table">
              <thead><tr><th>Tuya name</th><th>Product</th><th>Matched</th><th>IP</th><th>Current name</th></tr></thead>
              <tbody>
                {tuya.matches.map((m) => (
                  <tr key={m.mac} className={tuyaOnlyUnnamed && m.current_name ? 'identify-row-silent' : ''}>
                    <td><div className="watch-name">{m.tuya.name || <span className="text-dim">(no name in file)</span>}</div><div className="watch-sub">{m.mac}</div></td>
                    <td>{m.tuya.product_name || '-'}{m.tuya.category ? <span className="watch-sub"> [{m.tuya.category}]</span> : null}</td>
                    <td>by {m.matched_by}</td>
                    <td>{m.ip}</td>
                    <td>{m.current_name || <span className="text-dim">unnamed</span>}{m.current_nickname && m.current_nickname !== m.current_name ? <div className="watch-sub">eero: {m.current_nickname}</div> : null}</td>
                  </tr>
                ))}
                {tuya.unmatched.map((t) => (
                  <tr key={t.id} className="identify-row-silent">
                    <td><div className="watch-name">{t.name || t.id}</div><div className="watch-sub">{t.mac || 'no MAC in file'}</div></td>
                    <td>{t.product_name || '-'}</td>
                    <td className="status-red">no match</td>
                    <td>{t.ip || '-'}</td>
                    <td className="text-dim">not in inventory (offline, or MAC/IP missing from the file)</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="watch-section">
        <div className="section-header">
          <h2><Crosshair size={16} />Identify devices {identify?.active && <span className="badge">{identify.targets.length}</span>}</h2>
          {identify?.active ? (
            <button className="btn-action btn-sm" onClick={stopIdentify} disabled={busy === 'identify'}><Square size={14} /> Stop</button>
          ) : (
            <button className="btn-action btn-sm" onClick={startIdentify} disabled={busy === 'identify'}
              title="Probe every unnamed device every 2 s and show which one stops answering">
              <Crosshair size={14} /> Start identify mode
            </button>
          )}
        </div>
        {identify?.active ? (
          <div className="card">
            <p className="watch-hint">
              Probing {identify.targets.length} unnamed devices every {identify.interval_s}s
              {identify.elapsed_s != null ? ` · ${Math.floor(identify.elapsed_s / 60)}m ${identify.elapsed_s % 60}s` : ''}.
              Cut power to one device (air-gap tab on a smart switch, unplug it, or flip its breaker). The row that goes dark is it:
              type its name and save. The name is written to eero as the nickname too. Stops by itself after {Math.round(identify.auto_stop_s / 60)} minutes.
            </p>
            <table className="watch-table">
              <thead><tr><th></th><th>Device</th><th>IP</th><th>Vendor</th><th>Status</th><th>Name it</th></tr></thead>
              <tbody>
                {identify.targets.map((t) => (
                  <tr key={t.mac} className={t.dark ? 'identify-row-dark' : t.never_answered ? 'identify-row-silent' : ''}>
                    <td><span className={`identify-dot ${t.dark ? 'dark' : t.alive ? 'alive' : 'unknown'}`} /></td>
                    <td>
                      <div className="watch-name">{t.label || t.mac}</div>
                      <div className="watch-sub">{t.mac}</div>
                    </td>
                    <td>{t.ip}</td>
                    <td>{t.vendor || <span className="text-dim">unknown</span>}</td>
                    <td>
                      {t.dark ? <span className="status-red">dark for {t.dark_for_s}s</span>
                        : t.never_answered ? <span className="text-dim">never answered</span>
                        : t.alive ? <span className="status-green">answering</span>
                        : <span className="text-dim">waiting…</span>}
                    </td>
                    <td className="identify-name-cell">
                      <input
                        value={identifyNames[t.mac] ?? ''}
                        placeholder={t.dark ? 'e.g. Kitchen – Island dimmer' : ''}
                        onChange={(e) => setIdentifyNames({ ...identifyNames, [t.mac]: e.target.value })}
                        onKeyDown={(e) => { if (e.key === 'Enter') nameIdentified(t.mac); }}
                      />
                      <button className="btn-primary btn-sm" disabled={busy === `identify:${t.mac}` || !(identifyNames[t.mac] || '').trim()} onClick={() => nameIdentified(t.mac)}>Save</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="card empty-state">
            Start identify mode to work out which physical device each unnamed row is. Named devices are trusted and the name is written
            to eero as the device nickname, so the eero app shows it too.
            {!data.config.tuya_file && <> For Tuya / Smart Life / Feit devices, drop tinytuya's devices.json into the backend's data/tuya folder and a one-click name import appears here.</>}
          </div>
        )}
      </div>

      <Section title="Untrusted devices" icon={<ShieldAlert size={16} />} count={data.untrusted.length}
        empty="Every known device is trusted.">
        <DeviceTable
          devices={data.untrusted}
          busy={busy}
          probeAvailable={config.probe_available}
          onTrust={(d) => trust(d, true)}
          onBlock={block}
          onProbe={probe}
          onEdit={setEditing}
          onForget={forget}
        />
      </Section>

      <Section title={`New in the last ${config.new_days} days`} count={data.new.length}
        empty="No devices joined recently.">
        <DeviceTable
          devices={data.new}
          busy={busy}
          probeAvailable={config.probe_available}
          onTrust={(d) => trust(d, !d.trusted)}
          onBlock={block}
          onProbe={probe}
          onEdit={setEditing}
          onForget={forget}
        />
      </Section>

      <div className="watch-section">
        <div className="section-header">
          <h2><Network size={16} />LAN cross-reference <span className={`badge ${cross.lan_only.length ? '' : 'muted'}`}>{cross.lan_only.length}</span></h2>
        </div>
        {cross.last_scan ? (
          <>
            <p className="watch-cross-summary">
              Last sweep <span title={cross.last_scan.at}>{ago(cross.last_scan.at)}</span> from <strong>{cross.last_scan.source}</strong>
              {cross.last_scan.cidr ? ` on ${cross.last_scan.cidr}` : ''}: {cross.last_scan.count} answered on the wire.
              {' '}<strong>{cross.both}</strong> also reported by eero, <strong>{cross.eero_only}</strong> eero devices on that subnet were silent (usually asleep),
              {' '}<strong>{cross.lan_only.length}</strong> answered but eero does not list them.
              {cross.vendor_filled > 0 && <> Vendor filled from the OUI table for {cross.vendor_filled} devices eero left blank.</>}
              {!cross.oui_loaded && <> No OUI table loaded. <button className="btn-text" onClick={fetchOui} disabled={busy === 'oui'}>Download it</button>.</>}
            </p>
            {cross.lan_only.length === 0 ? (
              <div className="card empty-state">Everything on the wire is also in eero's list.</div>
            ) : (
              <div className="card">
                <DeviceTable
                  devices={cross.lan_only}
                  busy={busy}
                  probeAvailable={config.probe_available}
                  onTrust={(d) => trust(d, !d.trusted)}
                  onBlock={block}
                  onProbe={probe}
                  onEdit={setEditing}
                  onForget={forget}
                />
                <p className="watch-hint">
                  These answered ARP during the sweep while eero lists them as disconnected or not at all. A sleeping laptop or phone that
                  still answers ARP is the usual cause ("eero: offline" with a name). "eero: never" means eero has no record: a static-IP
                  device, a second router, something behind a non-eero AP, or a device eero cannot see.
                </p>
              </div>
            )}
          </>
        ) : (
          <div className="card empty-state">
            No LAN sweep yet. {cross.available
              ? 'Press LAN scan above, or set WATCH_LAN_SCAN=true to sweep on a schedule.'
              : 'This host cannot sweep (no ping or no default route). Push sweeps from netscan.py instead: netscan.py push --url <this dashboard>.'}
          </div>
        )}
      </div>

      <Section title="Top uploaders (24h)" count={data.uploaders.length}
        empty="Not enough samples yet. Uploads are evaluated after a few polls.">
        <table className="watch-table">
          <thead>
            <tr><th>Device</th><th>Uploading</th><th>Median up</th><th>Trusted</th></tr>
          </thead>
          <tbody>
            {data.uploaders.map((d) => (
              <tr key={d.mac} className={(d.upload?.ratio ?? 0) >= 0.9 ? 'watch-row-warn' : ''}>
                <td>
                  <div className="watch-name">{d.display_name}</div>
                  <div className="watch-sub">{d.mac} · {d.vendor || 'unknown vendor'}</div>
                </td>
                <td>{d.upload ? `${d.upload.uploading}/${d.upload.samples} samples (${Math.round(d.upload.ratio * 100)}%)` : '-'}</td>
                <td>{d.upload ? formatUp(d.upload.median_up) : '-'}</td>
                <td>{d.trusted ? <ShieldCheck size={14} className="status-green" /> : <ShieldAlert size={14} className="status-red" />}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="watch-hint">
          A device uploading in nearly every sample around the clock is the one signature a streaming camera cannot hide.
          Add a note starting with <code>ok:</code> to a trusted device to silence the rule for it.
        </p>
      </Section>

      <Section title="Recent events" count={data.events.length} empty="No events yet.">
        <ul className="watch-events">
          {data.events.map((e: WatchEvent) => (
            <li key={e.id} className={`watch-event watch-event-${e.type}`}>
              <span className="watch-event-type">{EVENT_LABEL[e.type] ?? e.type}</span>
              <span className="watch-event-detail">{e.mac ? `${e.mac}  ` : ''}{e.detail}</span>
              <span className="watch-event-at" title={e.at}>{ago(e.at)}</span>
            </li>
          ))}
        </ul>
      </Section>

      {editing && (
        <LabelDialog
          device={editing}
          onClose={() => setEditing(null)}
          onSave={(body) => run(`label:${editing.mac}`, () => saveLabel(editing.mac, body)).then(() => setEditing(null))}
        />
      )}

      {probeResult && createPortal(
        <div className="watch-dialog-backdrop" onClick={() => setProbeResult(null)}>
          <div className="watch-dialog card" onClick={(e) => e.stopPropagation()}>
            <h3>Probe {probeResult.result.ip ?? probeResult.mac}</h3>
            {probeResult.result.ok ? (
              <>
                <p className={probeResult.result.camera_like?.length ? 'status-red' : 'status-green'}>{probeResult.result.verdict}</p>
                <table className="watch-table">
                  <thead><tr><th>Port</th><th>State</th><th>Service</th><th>Version</th></tr></thead>
                  <tbody>
                    {(probeResult.result.ports ?? []).map((p) => (
                      <tr key={p.port} className={p.state === 'open' && p.hint ? 'watch-row-warn' : ''}>
                        <td>{p.port}</td><td>{p.state}</td><td>{p.service}{p.hint ? ` (${p.hint})` : ''}</td><td>{p.version}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : (
              <p className="status-red">{probeResult.result.error}</p>
            )}
            <div className="watch-dialog-actions">
              <button className="btn-action btn-sm" onClick={() => setProbeResult(null)}>Close</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

function formatUp(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)} G`;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)} M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)} K`;
  return v.toFixed(v < 10 ? 2 : 0);
}

function Stat({ label, value, sub, tone }: { label: string; value: number; sub?: string; tone?: 'red' | 'green' | 'yellow' | 'muted' }) {
  return (
    <div className={`card watch-stat ${tone ? `watch-stat-${tone}` : ''}`}>
      <div className="watch-stat-value">{value}</div>
      <div className="watch-stat-label">{label}</div>
      {sub && <div className="watch-stat-sub">{sub}</div>}
    </div>
  );
}

function Section({ title, icon, count, empty, children }: { title: string; icon?: React.ReactNode; count: number; empty: string; children: React.ReactNode }) {
  return (
    <div className="watch-section">
      <div className="section-header">
        <h2>{icon}{title} <span className={`badge ${count ? '' : 'muted'}`}>{count}</span></h2>
      </div>
      {count === 0 ? <div className="card empty-state">{empty}</div> : <div className="card">{children}</div>}
    </div>
  );
}

interface DeviceTableProps {
  devices: WatchDevice[];
  busy: string | null;
  probeAvailable: boolean;
  onTrust: (d: WatchDevice) => void;
  onBlock: (d: WatchDevice) => void;
  onProbe: (d: WatchDevice) => void;
  onEdit: (d: WatchDevice) => void;
  onForget: (d: WatchDevice) => void;
}

function DeviceTable({ devices, busy, probeAvailable, onTrust, onBlock, onProbe, onEdit, onForget }: DeviceTableProps) {
  return (
    <table className="watch-table">
      <thead>
        <tr><th>Device</th><th>IP</th><th>Vendor</th><th>First seen</th><th>Last seen</th><th>Status</th><th></th></tr>
      </thead>
      <tbody>
        {devices.map((d) => (
          <tr key={d.mac}>
            <td>
              <div className="watch-name">{d.display_name}</div>
              <div className="watch-sub">{d.mac}{d.hostname ? ` · ${d.hostname}` : ''}{d.owner ? ` · ${d.owner}` : ''}{d.segment ? ` · ${d.segment}` : ''}</div>
              {(d.lan_hostname || d.lan_last_seen) && (
                <div className="watch-sub">wire: {d.lan_hostname || d.lan_ip}{d.lan_last_seen ? ` · ${ago(d.lan_last_seen)}` : ''}{d.lan_source ? ` via ${d.lan_source}` : ''}</div>
              )}
              {d.probe && <div className="watch-sub">{d.probe.verdict}</div>}
            </td>
            <td>{d.last_ip || d.lan_ip || '-'}</td>
            <td>{d.vendor || d.manufacturer || <span className="text-dim">unknown</span>}{!d.manufacturer && d.vendor ? <span className="watch-sub"> (OUI)</span> : null}</td>
            <td title={d.first_seen}>{ago(d.first_seen)}</td>
            <td title={d.last_seen}>{ago(d.last_seen)}</td>
            <td>
              <span className={d.online ? 'status-green' : 'text-dim'}>{d.online ? 'eero: online' : d.first_source === 'lan' && !d.last_seen ? 'eero: never' : 'eero: offline'}</span>
              {d.lan_online && <>{' · '}<span className="status-green">wire: online</span></>}
              {' · '}
              <span className={d.trusted ? 'status-green' : 'status-red'}>{d.trusted ? 'trusted' : 'untrusted'}</span>
            </td>
            <td className="watch-actions">
              <button className="btn-icon-sm" title={d.trusted ? 'Mark untrusted' : 'Mark trusted'} disabled={busy === `trust:${d.mac}`} onClick={() => onTrust(d)}>
                {d.trusted ? <ShieldAlert size={14} /> : <ShieldCheck size={14} />}
              </button>
              <button className="btn-icon-sm" title="Name / owner / notes" onClick={() => onEdit(d)}><Pencil size={14} /></button>
              <button className="btn-icon-sm" title={probeAvailable ? 'Probe camera ports with nmap' : 'nmap not installed on the dashboard host'} disabled={!probeAvailable || busy === `probe:${d.mac}`} onClick={() => onProbe(d)}><Radar size={14} /></button>
              <button className="btn-icon-sm btn-icon-danger" title="Block on eero" disabled={busy === `block:${d.mac}`} onClick={() => onBlock(d)}><Ban size={14} /></button>
              <button className="btn-icon-sm btn-icon-danger" title="Forget (it will show as new if it returns)" disabled={busy === `forget:${d.mac}`} onClick={() => onForget(d)}><Trash2 size={14} /></button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LabelDialog({ device, onClose, onSave }: { device: WatchDevice; onClose: () => void; onSave: (body: { name: string; owner: string; notes: string; trusted: boolean; eero_nickname: boolean }) => void }) {
  const [name, setName] = useState(device.name);
  const [owner, setOwner] = useState(device.owner);
  const [notes, setNotes] = useState(device.notes);
  const [trusted, setTrusted] = useState(device.trusted);
  const [eeroNickname, setEeroNickname] = useState(!!device.device_url);
  return createPortal(
    <div className="watch-dialog-backdrop" onClick={onClose}>
      <div className="watch-dialog card" onClick={(e) => e.stopPropagation()}>
        <h3>{device.display_name || device.mac}</h3>
        <div className="watch-sub">{device.mac} · {device.vendor || device.manufacturer || 'unknown vendor'} · {device.last_ip || device.lan_ip}</div>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Living room Apple TV" /></label>
        <label>Owner<input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="house" /></label>
        <label>Notes<input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="ok: backup NAS uploads overnight" /></label>
        <label className="watch-check"><input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} /> Trusted</label>
        <label className="watch-check" title={device.device_url ? 'Writes the name to eero so the eero app shows it' : 'eero has no record of this device yet'}>
          <input type="checkbox" checked={eeroNickname} disabled={!device.device_url} onChange={(e) => setEeroNickname(e.target.checked)} />
          Also set as eero nickname{device.nickname ? ` (currently "${device.nickname}")` : ''}
        </label>
        <div className="watch-dialog-actions">
          <button className="btn-cancel btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn-primary btn-sm" onClick={() => onSave({ name, owner, notes, trusted, eero_nickname: eeroNickname && !!name.trim() })}>Save</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
