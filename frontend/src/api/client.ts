/**
 * Axios API client — centralized HTTP client with JWT interceptor.
 */

import axios from 'axios';
import { API_URL } from '../config';
import { useAuthStore } from '../store/authStore';

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

export const sessionsApi = {
  list: () => api.get<Session[]>('/sessions'),
  get: (id: string) => api.get<SessionDetail>(`/sessions/${id}`),
  create: (name: string, language: string) =>
    api.post<Session>('/sessions', { name, language }),
  addMember: (sessionId: string, userId: string, role: string) =>
    api.post(`/sessions/${sessionId}/members`, { user_id: userId, role }),
  updateRole: (sessionId: string, userId: string, role: string) =>
    api.patch(`/sessions/${sessionId}/members/${userId}`, { role }),
  removeMember: (sessionId: string, userId: string) =>
    api.delete(`/sessions/${sessionId}/members/${userId}`),
  close: (sessionId: string) => api.delete(`/sessions/${sessionId}`),
};
