import { useState } from 'react';
import type { FormEvent } from 'react';
import { useAuthStore } from '../../store/authStore';

interface Props {
  onSwitchToRegister: () => void;
}

export function LoginForm({ onSwitchToRegister }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const { login, isLoading, error } = useAuthStore();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await login(username, password);
    } catch {
      // Error is set in store
    }
  };

  return (
    <div className="auth-card">
      <h1>Welcome back</h1>
      <p className="subtitle">Sign in to Concord</p>

      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            className="input"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Enter your username"
            required
          />
        </div>

        <div className="form-group">
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter your password"
            required
          />
        </div>

        {error && <p className="form-error">{error}</p>}

        <button className="btn btn-primary w-full" type="submit" disabled={isLoading}>
          {isLoading ? 'Signing in…' : 'Sign in'}
        </button>
      </form>

      <p className="auth-link">
        Don't have an account?{' '}
        <a href="#" onClick={(e) => { e.preventDefault(); onSwitchToRegister(); }}>
          Create one
        </a>
      </p>
    </div>
  );
}
