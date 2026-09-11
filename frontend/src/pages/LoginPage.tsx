import { useState } from 'react';
import { LoginForm } from '../components/Auth/LoginForm';
import { RegisterForm } from '../components/Auth/RegisterForm';

export function LoginPage() {
  const [mode, setMode] = useState<'login' | 'register'>('login');

  return (
    <div className="auth-page">
      {mode === 'login' ? (
        <LoginForm onSwitchToRegister={() => setMode('register')} />
      ) : (
        <RegisterForm onSwitchToLogin={() => setMode('login')} />
      )}
    </div>
  );
}
