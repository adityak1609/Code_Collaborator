import { useState } from 'react';
import type { FormEvent } from 'react';
import { useAuthStore } from '../../store/authStore';

interface Props {
  onSwitchToLogin: () => void;
}

export function RegisterForm({ onSwitchToLogin }: Props) {
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const { register, isLoading, error } = useAuthStore();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await register(username, email, password);
    } catch {
      // Error is set in store
    }
  };

  return (
    <div className="auth-card">
      <h1>Create account</h1>
      <p className="subtitle">Join Concord to start collaborating</p>

      <form onSubmit={handleSubmit}>
        <div className="form-group">
          <label htmlFor="register-username">Username</label>
          <input
            id="register-username"
            className="input"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Choose a username"
            required
            minLength={3}
          />
        </div>

        <div className="form-group">
          <label htmlFor="register-email">Email</label>
          <input
            id="register-email"
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            required
          />
        </div>

        <div className="form-group">
          <label htmlFor="register-password">Password</label>
          <input
            id="register-password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Min 6 characters"
            required
            minLength={6}
          />
        </div>

        {error && <p className="form-error">{error}</p>}

        <button className="btn btn-primary w-full" type="submit" disabled={isLoading}>
          {isLoading ? 'Creating account…' : 'Create account'}
        </button>
      </form>

      <p className="auth-link">
        Already have an account?{' '}
        <a href="#" onClick={(e) => { e.preventDefault(); onSwitchToLogin(); }}>
          Sign in
        </a>
      </p>
    </div>
  );
}
