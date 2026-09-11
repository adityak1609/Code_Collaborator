/**
 * Zustand auth store — holds JWT, user info, and auth actions.
 * Token stored in memory only (not localStorage) for security.
 */

import { create } from 'zustand';
import axios from 'axios';
import { API_URL } from '../config';

export interface User {
  id: string;
  username: string;
  email: string;
  created_at: string;
}

interface AuthState {
  token: string | null;
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  login: (username: string, password: string) => Promise<void>;
  register: (username: string, email: string, password: string) => Promise<void>;
  logout: () => void;
  fetchMe: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  isAuthenticated: false,
  isLoading: false,
  error: null,

  login: async (username: string, password: string) => {
    set({ isLoading: true, error: null });
    try {
      const res = await axios.post(`${API_URL}/auth/login`, { username, password });
      const token = res.data.access_token;
      set({ token, isAuthenticated: true, isLoading: false });
      // Fetch user profile
      await get().fetchMe();
    } catch (err: any) {
      const detail = err.response?.data?.detail || 'Login failed';
      set({ isLoading: false, error: detail });
      throw err;
    }
  },

  register: async (username: string, email: string, password: string) => {
    set({ isLoading: true, error: null });
    try {
      await axios.post(`${API_URL}/auth/register`, { username, email, password });
      // Auto-login after register
      await get().login(username, password);
    } catch (err: any) {
      const detail = err.response?.data?.detail || 'Registration failed';
      set({ isLoading: false, error: detail });
      throw err;
    }
  },

  logout: () => {
    set({ token: null, user: null, isAuthenticated: false, error: null });
  },

  fetchMe: async () => {
    const { token } = get();
    if (!token) return;
    try {
      const res = await axios.get(`${API_URL}/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      set({ user: res.data });
    } catch {
      set({ token: null, user: null, isAuthenticated: false });
    }
  },
}));
