/**
 * Axios API client — centralized HTTP client with JWT interceptor.
 */

import axios from 'axios';
import { API_URL } from '../config';
import { useAuthStore } from '../store/authStore';
import type { ExecutionHistory, ExecutionRecord } from '../types/execution';

const api = axios.create({
  baseURL: API_URL,
  headers: { 'Content-Type': 'application/json' },
});

// Attach JWT to every request
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle 401 — auto logout
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      useAuthStore.getState().logout();
    }
    return Promise.reject(error);
  }
);

export default api;

// ── Session API ──────────────────────────────────

export interface Session {
  id: string;
  name: string;
  language: 'python' | 'cpp' | 'javascript';
  owner_id: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Member {
  user_id: string;
  username: string;
  role: 'viewer' | 'editor' | 'owner';
  joined_at: string;
}

export interface SessionDetail extends Session {
  members: Member[];
}

export interface DocumentSaveResult {
  session_id: string;
  size_bytes: number;
  saved_at: string;
  state_vector: string;
  state_hash: string;
  dirty: boolean;
}

export interface Snapshot {
  id: string;
  session_id: string;
  created_by: string | null;
  label: string;
  size_bytes: number;
  created_at: string;
}

export interface SnapshotHistory {
  items: Snapshot[];
  total: number;
  limit: number;
  offset: number;
}

export interface SnapshotRestoreResult {
  snapshot: Snapshot;
  update_size_bytes: number;
}

export const sessionsApi = {
  list: () => api.get<Session[]>('/sessions'),
  get: (id: string) => api.get<SessionDetail>(`/sessions/${id}`),
  save: (id: string, documentState: Uint8Array) =>
    api.post<DocumentSaveResult>(`/sessions/${id}/save`, documentState, {
      headers: { 'Content-Type': 'application/octet-stream' },
    }),
  create: (name: string, language: string) =>
    api.post<Session>('/sessions', { name, language }),
  addMember: (sessionId: string, username: string, role: 'viewer' | 'editor') =>
    api.post(`/sessions/${sessionId}/members`, { username, role }),
  updateRole: (sessionId: string, userId: string, role: string) =>
    api.patch(`/sessions/${sessionId}/members/${userId}`, { role }),
  removeMember: (sessionId: string, userId: string) =>
    api.delete(`/sessions/${sessionId}/members/${userId}`),
  close: (sessionId: string) => api.delete(`/sessions/${sessionId}`),
};

export const executionsApi = {
  run: (sessionId: string) =>
    api.post<ExecutionRecord>(`/sessions/${sessionId}/run`),
  list: (sessionId: string, limit = 20, offset = 0) =>
    api.get<ExecutionHistory>(`/sessions/${sessionId}/executions`, {
      params: { limit, offset },
    }),
  get: (sessionId: string, executionId: string) =>
    api.get<ExecutionRecord>(
      `/sessions/${sessionId}/executions/${executionId}`,
    ),
  cancel: (sessionId: string, executionId: string) =>
    api.post<ExecutionRecord>(
      `/sessions/${sessionId}/executions/${executionId}/cancel`,
    ),
};

export const snapshotsApi = {
  list: (sessionId: string, limit = 20, offset = 0) =>
    api.get<SnapshotHistory>(`/sessions/${sessionId}/snapshots`, {
      params: { limit, offset },
    }),
  create: (sessionId: string, label: string) =>
    api.post<Snapshot>(`/sessions/${sessionId}/snapshots`, { label }),
  restore: (sessionId: string, snapshotId: string) =>
    api.post<SnapshotRestoreResult>(
      `/sessions/${sessionId}/snapshots/${snapshotId}/restore`,
    ),
};
