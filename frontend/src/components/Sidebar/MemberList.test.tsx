import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MemberList } from './MemberList';

const joinedAt = '2026-09-13T08:00:00Z';

describe('MemberList', () => {
  it('labels the current user and distinguishes online and offline members', () => {
    const { container } = render(
      <MemberList
        currentUserId="owner-id"
        members={[
          {
            user_id: 'owner-id',
            username: 'ada',
            role: 'owner',
            joined_at: joinedAt,
          },
          {
            user_id: 'viewer-id',
            username: 'grace',
            role: 'viewer',
            joined_at: joinedAt,
          },
        ]}
        onlineUsers={[
          {
            userId: 'owner-id',
            name: 'ada',
            color: '#ff00aa',
          },
        ]}
      />,
    );

    expect(screen.getByText('Members (2)')).toBeInTheDocument();
    expect(screen.getByText('ada (you)')).toBeInTheDocument();
    expect(screen.getByText('grace')).toBeInTheDocument();
    expect(screen.getByText('owner')).toHaveClass('badge-owner');
    expect(screen.getByText('viewer')).toHaveClass('badge-viewer');

    const dots = container.querySelectorAll<HTMLElement>('.presence-dot');
    expect(dots[0]).toHaveStyle({ backgroundColor: '#ff00aa', opacity: '1' });
    expect(dots[1]).toHaveStyle({ backgroundColor: '#6e7681', opacity: '0.4' });
  });

  it('lets an owner invite, change roles, and remove non-owner members', async () => {
    const onAddMember = vi.fn().mockResolvedValue(true);
    const onUpdateRole = vi.fn().mockResolvedValue(true);
    const onRemoveMember = vi.fn().mockResolvedValue(true);
    render(
      <MemberList
        currentUserId="owner-id"
        canManage
        members={[
          {
            user_id: 'owner-id',
            username: 'ada',
            role: 'owner',
            joined_at: joinedAt,
          },
          {
            user_id: 'editor-id',
            username: 'grace',
            role: 'editor',
            joined_at: joinedAt,
          },
        ]}
        onlineUsers={[]}
        onAddMember={onAddMember}
        onUpdateRole={onUpdateRole}
        onRemoveMember={onRemoveMember}
      />,
    );

    fireEvent.change(screen.getByLabelText('Invite by username'), {
      target: { value: 'linus' },
    });
    fireEvent.change(screen.getByLabelText('Invitation role'), {
      target: { value: 'viewer' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }));
    await waitFor(() =>
      expect(onAddMember).toHaveBeenCalledWith('linus', 'viewer'),
    );

    fireEvent.change(screen.getByLabelText('Role for grace'), {
      target: { value: 'viewer' },
    });
    expect(onUpdateRole).toHaveBeenCalledWith('editor-id', 'viewer');

    fireEvent.click(screen.getByRole('button', { name: 'Remove grace' }));
    expect(onRemoveMember).toHaveBeenCalledWith('editor-id');
    expect(screen.queryByRole('button', { name: 'Remove ada' })).toBeNull();
  });
});
