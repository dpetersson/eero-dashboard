import { useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import { ShieldAlert, ShieldCheck, RefreshCw, Radar, Ban, Pencil, Trash2 } from 'lucide-react';

import * as api from '../api';
import { useFetch } from '../hooks/useFetch';
import type { WatchDevice, WatchEvent, WatchSummary } from '../api/watch';

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

  if (loading && !data) return <div className="card loading-card"><div className="spinner" /> Loading watch data…</div>;
  if (error && !data) return <div className="card error-card">Watch data unavailable: {error}</div>;
  if (!data) return null;

  const { counts, config } = data;

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
          {counts.untrusted > 0 && (
            <button className="btn-action btn-sm" onClick={trustAll} disabled={busy === 'trust-all'} title="Mark every known device trusted">
              <ShieldCheck size={14} /> Trust all
            </button>
          )}
        </div>
      </div>

      {actionError && <div className="card error-card">{actionError}</div>}

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
                  <div className="watch-sub">{d.mac} · {d.manufacturer || 'unknown vendor'}</div>
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
          onSave={(body) => run(`label:${editing.mac}`, () => api.labelWatchDevice(networkId, editing.mac, body)).then(() => setEditing(null))}
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
              <div className="watch-sub">{d.mac}{d.hostname ? ` · ${d.hostname}` : ''}{d.owner ? ` · ${d.owner}` : ''}</div>
              {d.probe && <div className="watch-sub">{d.probe.verdict}</div>}
            </td>
            <td>{d.last_ip || '-'}</td>
            <td>{d.manufacturer || <span className="text-dim">unknown</span>}</td>
            <td title={d.first_seen}>{ago(d.first_seen)}</td>
            <td title={d.last_seen}>{ago(d.last_seen)}</td>
            <td>
              <span className={d.online ? 'status-green' : 'text-dim'}>{d.online ? 'online' : 'offline'}</span>
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

function LabelDialog({ device, onClose, onSave }: { device: WatchDevice; onClose: () => void; onSave: (body: { name: string; owner: string; notes: string; trusted: boolean }) => void }) {
  const [name, setName] = useState(device.name);
  const [owner, setOwner] = useState(device.owner);
  const [notes, setNotes] = useState(device.notes);
  const [trusted, setTrusted] = useState(device.trusted);
  return createPortal(
    <div className="watch-dialog-backdrop" onClick={onClose}>
      <div className="watch-dialog card" onClick={(e) => e.stopPropagation()}>
        <h3>{device.display_name || device.mac}</h3>
        <div className="watch-sub">{device.mac} · {device.manufacturer || 'unknown vendor'} · {device.last_ip}</div>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Living room Apple TV" /></label>
        <label>Owner<input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="house" /></label>
        <label>Notes<input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="ok: backup NAS uploads overnight" /></label>
        <label className="watch-check"><input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} /> Trusted</label>
        <div className="watch-dialog-actions">
          <button className="btn-cancel btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn-primary btn-sm" onClick={() => onSave({ name, owner, notes, trusted })}>Save</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
