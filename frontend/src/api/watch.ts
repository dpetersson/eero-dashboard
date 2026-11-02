import { request } from './client';

export interface WatchUpload {
  samples: number;
  uploading: number;
  ratio: number;
  median_up: number;
}

export interface WatchProbePort {
  port: number;
  state: string;
  service: string;
  version: string;
  hint: string;
}

export interface WatchProbe {
  ok: boolean;
  error?: string;
  ip?: string;
  ports?: WatchProbePort[];
  open_count?: number;
  camera_like?: number[];
  verdict?: string;
}

export interface WatchDevice {
  mac: string;
  network_id: string;
  name: string;
  owner: string;
  notes: string;
  trusted: boolean;
  first_seen: string;
  last_seen: string;
  last_ip: string;
  hostname: string;
  nickname: string;
  manufacturer: string;
  device_type: string;
  connection: string;
  device_url: string;
  probe: WatchProbe | null;
  probe_at: string | null;
  display_name?: string;
  is_new?: boolean;
  online?: boolean;
  upload?: WatchUpload | null;
  samples?: { seen_at: string; ip: string; up: number | null; down: number | null }[];
}

export interface WatchEvent {
  id: number;
  at: string;
  network_id: string;
  type: string;
  mac: string;
  detail: string;
}

export interface WatchSummary {
  counts: { total: number; online: number; untrusted: number; untrusted_online: number; new: number };
  untrusted: WatchDevice[];
  new: WatchDevice[];
  uploaders: WatchDevice[];
  events: WatchEvent[];
  config: {
    poll_seconds: number;
    new_days: number;
    webhook_configured: boolean;
    probe_available: boolean;
    lan_only: boolean;
    allowed_cidrs: string[];
  };
}

export const getWatchSummary = (networkId: string) =>
  request<WatchSummary>(`/networks/${networkId}/watch/summary`);

export const getWatchDevices = (networkId: string) =>
  request<{ devices: WatchDevice[] }>(`/networks/${networkId}/watch/devices`);

export const getWatchDevice = (networkId: string, mac: string) =>
  request<WatchDevice>(`/networks/${networkId}/watch/devices/${encodeURIComponent(mac)}`);

export const labelWatchDevice = (
  networkId: string,
  mac: string,
  body: { name?: string; owner?: string; notes?: string; trusted?: boolean },
) =>
  request<WatchDevice>(`/networks/${networkId}/watch/devices/${encodeURIComponent(mac)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

export const forgetWatchDevice = (networkId: string, mac: string) =>
  request<{ deleted: string }>(`/networks/${networkId}/watch/devices/${encodeURIComponent(mac)}`, {
    method: 'DELETE',
  });

export const probeWatchDevice = (networkId: string, mac: string) =>
  request<WatchProbe>(`/networks/${networkId}/watch/devices/${encodeURIComponent(mac)}/probe`, {
    method: 'POST',
  });

export const runWatchPoll = (networkId: string) =>
  request<{ results: unknown[] }>(`/networks/${networkId}/watch/poll`, { method: 'POST' });

export const trustAllWatch = (networkId: string) =>
  request<{ trusted: number }>(`/networks/${networkId}/watch/baseline`, { method: 'POST' });
