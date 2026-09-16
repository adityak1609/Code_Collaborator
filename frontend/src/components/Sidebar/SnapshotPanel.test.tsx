import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { snapshotsApi } from '../../api/client';
import { SnapshotPanel } from './SnapshotPanel';

vi.mock('../../api/client', () => ({
  snapshotsApi: {
    list: vi.fn(),
    create: vi.fn(),
    restore: vi.fn(),
  },
}));

const snapshot = {
  id: 'snapshot-1',
  session_id: 'session-1',
  created_by: 'user-1',
  label: 'Before refactor',
  size_bytes: 2048,
  created_at: '2026-09-17T10:00:00Z',
};

describe('SnapshotPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.mocked(snapshotsApi.list).mockResolvedValue({
      data: { items: [snapshot], total: 1, limit: 20, offset: 0 },
    } as never);
  });

  it('lets editors create and restore named snapshots', async () => {
    vi.mocked(snapshotsApi.create).mockResolvedValue({
      data: { ...snapshot, id: 'snapshot-2', label: 'Stable parser' },
    } as never);
    vi.mocked(snapshotsApi.restore).mockResolvedValue({
      data: { snapshot, update_size_bytes: 32 },
    } as never);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const restored = vi.fn();
    render(
      <SnapshotPanel sessionId="session-1" canEdit onRestored={restored} />,
    );

    expect(await screen.findByText('Before refactor')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Snapshot label'), {
      target: { value: 'Stable parser' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(await screen.findByText('Stable parser')).toBeInTheDocument();
    expect(snapshotsApi.create).toHaveBeenCalledWith('session-1', 'Stable parser');

    fireEvent.click(screen.getAllByRole('button', { name: 'Restore' })[1]);
    await waitFor(() => expect(restored).toHaveBeenCalledWith(snapshot));
    expect(snapshotsApi.restore).toHaveBeenCalledWith('session-1', 'snapshot-1');
  });

  it('gives viewers history without mutation controls', async () => {
    render(<SnapshotPanel sessionId="session-1" canEdit={false} />);
    expect(await screen.findByText('Before refactor')).toBeInTheDocument();
    expect(screen.queryByLabelText('Snapshot label')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument();
  });
});
