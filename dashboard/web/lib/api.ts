// ---------------------------------------------------------------------------
// API types
// ---------------------------------------------------------------------------

export interface Alert {
  id: string;
  type: string;
  severity: 'info' | 'warning' | 'error';
  message: string;
}

export interface Recommendation {
  action: string;
  reason: string;
  confidence: number;
  low_confidence?: boolean;
}

export interface LastNight {
  date: string;
  total_sleep_min: number;
  deep_min: number;
  rem_min: number;
  wake_events: number;
  sleep_efficiency: number;
  avg_hrv: number;
  outcome_score: number;
}

export interface Schedule {
  required_wake_time: string;
  sleep_opportunity_min: number;
  is_short_sleep_day: boolean;
}

export interface StatusResponse {
  state: string;
  objective: string;
  mode: 'auto' | 'manual' | 'view';
  target_temp_f: number;
  bed_temp_f: number;
  room_temp_f: number;
  stage: string;
  confidence: number;
  daemon_alive: boolean;
  stale: boolean;
  updated: string;
  recommendation: Recommendation;
  last_night: LastNight | null;
  alerts: Alert[];
  schedule: Schedule | null;
}

export interface TonightResponse {
  mode: 'auto' | 'manual' | 'view';
  state: string;
  target_temp_f: number;
  schedule: Schedule | null;
  recommendation: Recommendation;
  setpoint: SetpointInfo | null;
}

export interface SetpointInfo {
  version: string;
  source: string;
  neutral_f: number;
  deep_bias_f: number;
  rem_warm_offset_f: number;
  wake_ramp_f: number;
  composite_bed_weight: number;
}

export interface NightSummary {
  date: string;
  total_sleep_min: number;
  deep_min: number;
  rem_min: number;
  wake_events: number;
  sleep_efficiency: number;
  avg_hrv: number;
  outcome_score: number;
}

export interface NightSample {
  ts: string;
  stage: string;
  heart_rate: number;
  hrv: number;
  bed_temp_f: number;
  room_temp_f: number;
}

export interface Intervention {
  id?: string;
  date: string;
  action: string;
  source: string;
  confidence: number;
  reward?: number;
}

export interface Note {
  date: string;
  text: string;
}

export interface MLOverview {
  baselines: Record<string, number>;
  setpoint: SetpointInfo;
  model_confidence: number;
  clean_nights: number;
  min_nights: number;
  recommendation: Recommendation;
  actions: Array<{
    date: string;
    action: string;
    source: string;
    confidence: number;
    reward: number;
  }>;
  phenotype: Array<{ feature: string; r: number; n: number }>;
}

export interface TrendPoint {
  date: string;
  value: number;
}

export interface TrendsResponse {
  metric: string;
  points: TrendPoint[];
}

export interface EffectivenessResponse {
  by_action: Array<{ action: string; n: number; mean_reward: number }>;
}

export interface SettingsResponse {
  stored: Record<string, number | string | boolean>;
  defaults: {
    neutral_temp_f: number;
    deep_bias_temp_f: number;
    wake_ramp_temp_f: number;
    wake_window_min: number;
    wake_vibration_power: number;
    max_step_f: number;
    hrv_target_ms: number;
    wake_events_max: number;
  };
}

export interface AdminHealth {
  daemon: { alive: boolean; updated: string; stale: boolean };
  sources: Record<string, { ok: boolean; last_ok?: string; error?: string }>;
  pending_commands: number;
}

export interface LogEntry {
  ts: string;
  level: string;
  message: string;
}

export interface AuthUser {
  username: string;
  display_name?: string;
}

export interface CommandResponse {
  queued: boolean;
  command_id: string;
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const res = await fetch(path, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string>),
    },
    ...options,
  });

  if (res.status === 401) {
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
    throw new ApiError(401, 'Unauthorized');
  }

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text);
  }

  // 204 No Content
  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

export const api = {
  // Auth
  login: (username: string, password: string) =>
    apiFetch<{ token: string; user: AuthUser }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  logout: () => apiFetch<void>('/api/auth/logout', { method: 'POST' }),

  me: () => apiFetch<{ user: AuthUser }>('/api/auth/me'),

  // Status
  status: () => apiFetch<StatusResponse>('/api/status'),

  // Tonight
  tonight: () => apiFetch<TonightResponse>('/api/tonight'),

  setTemp: (target_f: number) =>
    apiFetch<void>('/api/tonight/temp', {
      method: 'POST',
      body: JSON.stringify({ target_f }),
    }),

  setMode: (mode: 'auto' | 'manual' | 'view') =>
    apiFetch<void>('/api/tonight/mode', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),

  setWake: (wake_time: string, window_min?: number) =>
    apiFetch<void>('/api/tonight/wake', {
      method: 'POST',
      body: JSON.stringify({ wake_time, window_min }),
    }),

  // Control
  control: (cmd: 'start' | 'pause' | 'resume' | 'stop' | 'safe-default') =>
    apiFetch<CommandResponse>(`/api/control/${cmd}`, { method: 'POST' }),

  // Nights
  nights: (limit = 30) =>
    apiFetch<NightSummary[]>(`/api/nights?limit=${limit}`),

  night: (date: string) => apiFetch<NightSummary>(`/api/nights/${date}`),

  nightSamples: (date: string) =>
    apiFetch<NightSample[]>(`/api/nights/${date}/samples`),

  // Interventions
  interventions: (limit = 50) =>
    apiFetch<Intervention[]>(`/api/interventions?limit=${limit}`),

  // Notes
  notes: (date?: string) =>
    apiFetch<Note[]>(date ? `/api/notes?date=${date}` : '/api/notes'),

  saveNote: (date: string, text: string) =>
    apiFetch<Note>('/api/notes', {
      method: 'POST',
      body: JSON.stringify({ date, text }),
    }),

  // ML
  mlOverview: () => apiFetch<MLOverview>('/api/ml/overview'),

  // Analytics
  trends: (metric: string, window = 30) =>
    apiFetch<TrendsResponse>(
      `/api/analytics/trends?metric=${metric}&window=${window}`
    ),

  effectiveness: () =>
    apiFetch<EffectivenessResponse>('/api/analytics/effectiveness'),

  // Settings
  settings: () => apiFetch<SettingsResponse>('/api/settings'),

  saveSettings: (values: Record<string, number | string | boolean>) =>
    apiFetch<SettingsResponse>('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ values }),
    }),

  // Admin
  adminHealth: () => apiFetch<AdminHealth>('/api/admin/health'),

  adminLogs: (limit = 50) =>
    apiFetch<LogEntry[]>(`/api/admin/logs?limit=${limit}`),

  // Alerts
  alerts: () => apiFetch<Alert[]>('/api/alerts'),

  ackAlert: (id: string) =>
    apiFetch<void>(`/api/alerts/${id}/ack`, { method: 'POST' }),
};

// SWR fetcher
export const fetcher = (url: string) =>
  fetch(url, { credentials: 'include' }).then((r) => {
    if (r.status === 401) {
      if (typeof window !== 'undefined') window.location.href = '/login';
      throw new Error('Unauthorized');
    }
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  });
