import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LoginPage } from '../../pages/LoginPage';
import { useAuthStore } from '../../store/authStore';

describe('LoginPage', () => {
  beforeEach(() => {
    useAuthStore.setState({
      token: null,
      user: null,
      isAuthenticated: false,
      isLoading: false,
      error: null,
    });
  });

  it('switches to registration and submits the entered account details', async () => {
    const register = vi.fn().mockResolvedValue(undefined);
    useAuthStore.setState({ register });
    render(<LoginPage />);

    fireEvent.click(screen.getByRole('link', { name: 'Create one' }));
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'test_user' },
    });
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'test@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'password123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => {
      expect(register).toHaveBeenCalledWith(
        'test_user',
        'test@example.com',
        'password123',
      );
    });
  });

  it('renders an authentication error from the store', () => {
    useAuthStore.setState({ error: 'Invalid username or password' });
    render(<LoginPage />);

    expect(screen.getByText('Invalid username or password')).toBeInTheDocument();
  });
});
