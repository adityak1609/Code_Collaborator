import { useEffect } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { executionsApi, sessionsApi } from '../api/client';
import { useAuthStore } from '../store/authStore';
import { SessionPage } from './SessionPage';

vi.mock('../api/client', () => ({
  sessionsApi: {
    get: vi.fn(),
    save: vi.fn(),
  },
  executionsApi: {
    run: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    cancel: vi.fn(),
  },
  snapshotsApi: {
    list: vi.fn(),
    create: vi.fn(),
    restore: vi.fn(),
  },
}));

vi.mock('../components/Editor/CollaborativeEditor', () => ({
  CollaborativeEditor: (props: {
    role: 'viewer' | 'editor' | 'owner';
    onAuthorizationChange?: () => void;
    onDocumentReady?: (getState: (() => Uint8Array) | null) => void;
    onDocumentChange?: () => void;
  }) => {
    const { onDocumentReady } = props;
    useEffect(() => {
      onDocumentReady?.(() => new Uint8Array([1, 2, 3]));
      return () => onDocumentReady?.(null);
    }, [onDocumentReady]);

    return (
      <div>
        <span data-testid="editor-role">{props.role}</span>
        <button onClick={props.onDocumentChange}>Edit document</button>
        <button onClick={props.onAuthorizationChange}>Invalidate authorization</button>
      </div>
    );
  },
}));

const ownerSession = {
  id: 'session-id',
  name: 'Pairing session',
  language: 'python' as const,
  owner_id: 'owner-id',
  is_active: true,
  created_at: '2026-09-13T08:00:00Z',
  updated_at: '2026-09-13T08:00:00Z',
  members: [
    {
      user_id: 'owner-id',
      username: 'ada',
      role: 'owner' as const,
      joined_at: '2026-09-13T08:00:00Z',
    },
  ],
};

const renderSessionPage = () =>
  render(
    <MemoryRouter initialEntries={['/session/session-id']}>
      <Routes>
        <Route path="/session/:sessionId" element={<SessionPage />} />
      </Routes>
    </MemoryRouter>,
  );

describe('SessionPage', () => {
  beforeEach(() => {
    vi.mocked(sessionsApi.get).mockReset();
    vi.mocked(sessionsApi.save).mockReset();
    vi.mocked(executionsApi.run).mockReset();
    vi.mocked(executionsApi.list).mockReset();
    vi.mocked(executionsApi.get).mockReset();
    vi.mocked(executionsApi.cancel).mockReset();
    vi.mocked(executionsApi.list).mockResolvedValue({
      data: { items: [], total: 0, limit: 20, offset: 0 },
    } as never);
    useAuthStore.setState({
      token: 'token',
      user: {
        id: 'owner-id',
        username: 'ada',
        email: 'ada@example.com',
        created_at: '2026-09-13T08:00:00Z',
      },
      isAuthenticated: true,
      isLoading: false,
      error: null,
    });
  });

  it('saves a synchronized document and reports the saved state', async () => {
    vi.mocked(sessionsApi.get).mockResolvedValue({ data: ownerSession } as never);
    vi.mocked(sessionsApi.save).mockResolvedValue({
      data: {
        session_id: 'session-id',
        size_bytes: 3,
        saved_at: '2026-09-13T08:05:00Z',
        state_vector: 'AQ==',
        state_hash: 'a'.repeat(64),
        dirty: false,
      },
    } as never);
    renderSessionPage();

    expect(await screen.findByText('Pairing session')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Edit document' }));
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();

    const saveButton = screen.getByRole('button', { name: 'Save' });
    await waitFor(() => expect(saveButton).toBeEnabled());
    fireEvent.click(saveButton);

    await waitFor(() => expect(sessionsApi.save).toHaveBeenCalledOnce());
    expect(sessionsApi.save).toHaveBeenCalledWith(
      'session-id',
      new Uint8Array([1, 2, 3]),
    );
    expect(await screen.findByText(/^Saved /)).toBeInTheDocument();
  });

  it('remounts immediately as read-only after authorization invalidation', async () => {
    vi.mocked(sessionsApi.get)
      .mockResolvedValueOnce({ data: ownerSession } as never)
      .mockResolvedValueOnce({
        data: {
          ...ownerSession,
          members: [{ ...ownerSession.members[0], role: 'viewer' }],
        },
      } as never);
    renderSessionPage();

    expect(await screen.findByTestId('editor-role')).toHaveTextContent('owner');
    fireEvent.click(
      screen.getByRole('button', { name: 'Invalidate authorization' }),
    );

    expect(screen.getByTestId('editor-role')).toHaveTextContent('viewer');
    await waitFor(() => expect(sessionsApi.get).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.getByText('Read-only')).toBeInTheDocument();
  });

  it('runs and cancels the current collaborative draft', async () => {
    vi.mocked(sessionsApi.get).mockResolvedValue({ data: ownerSession } as never);
    const running = {
      id: 'execution-id',
      session_id: 'session-id',
      triggered_by: 'owner-id',
      status: 'RUNNING' as const,
      code: 'print("hi")',
      language: 'python' as const,
      stdout: '',
      stderr: '',
      exit_code: null,
      elapsed_ms: null,
      created_at: '2026-09-13T08:06:00Z',
      finished_at: null,
    };
    vi.mocked(executionsApi.run).mockResolvedValue({ data: running } as never);
    vi.mocked(executionsApi.cancel).mockResolvedValue({
      data: { ...running, status: 'CANCELLED', finished_at: '2026-09-13T08:06:01Z' },
    } as never);
    vi.mocked(executionsApi.get).mockResolvedValue({ data: running } as never);
    renderSessionPage();

    const runButton = await screen.findByRole('button', { name: /run/i });
    await waitFor(() => expect(runButton).toBeEnabled());
    fireEvent.click(runButton);

    await waitFor(() => expect(executionsApi.run).toHaveBeenCalledWith('session-id'));
    expect(await screen.findByText('RUNNING')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    await waitFor(() => expect(executionsApi.cancel).toHaveBeenCalledWith(
      'session-id',
      'execution-id',
    ));
    expect(await screen.findByText('CANCELLED')).toBeInTheDocument();
  });
});
