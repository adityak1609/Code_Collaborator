/**
 * MemberList — displays session members with presence indicators.
 */

import type { Member } from '../../api/client';
import type { PresenceUser } from '../../types/collaboration';

interface Props {
  members: Member[];
  onlineUsers: PresenceUser[];
  currentUserId: string;
}

export function MemberList({ members, onlineUsers, currentUserId }: Props) {
  const onlineIds = new Set(onlineUsers.map((u) => u.userId));

  return (
    <div className="sidebar-section">
      <h3>Members ({members.length})</h3>
      {members.map((member) => {
        const isOnline = onlineIds.has(member.user_id);
        const isSelf = member.user_id === currentUserId;
        const onlineUser = onlineUsers.find((u) => u.userId === member.user_id);

        return (
          <div key={member.user_id} className="member-item">
            <span
              className="presence-dot"
              style={{
                backgroundColor: isOnline
                  ? (onlineUser?.color || '#3fb950')
                  : '#6e7681',
                animation: isOnline ? undefined : 'none',
                opacity: isOnline ? 1 : 0.4,
              }}
            />
            <span className="name">
              {member.username}
              {isSelf && ' (you)'}
            </span>
            <span className={`badge badge-${member.role}`}>
              {member.role}
            </span>
          </div>
        );
      })}
    </div>
  );
}
