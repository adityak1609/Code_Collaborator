import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
});
