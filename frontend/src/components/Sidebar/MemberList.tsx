/**
 * MemberList — displays session members with presence indicators.
 */

import { useState } from 'react';
import type { FormEvent } from 'react';
import type { Member } from '../../api/client';
import type { PresenceUser } from '../../types/collaboration';

type AssignableRole = 'viewer' | 'editor';

interface Props {
  members: Member[];
  onlineUsers: PresenceUser[];
  currentUserId: string;
  canManage?: boolean;
  actionKey?: string | null;
  feedback?: { kind: 'success' | 'error'; message: string } | null;
  onAddMember?: (username: string, role: AssignableRole) => Promise<boolean>;
  onUpdateRole?: (userId: string, role: AssignableRole) => Promise<boolean>;
  onRemoveMember?: (userId: string) => Promise<boolean>;
}

export function MemberList({
  members,
  onlineUsers,
  currentUserId,
  canManage = false,
  actionKey = null,
  feedback = null,
  onAddMember,
  onUpdateRole,
  onRemoveMember,
}: Props) {
  const onlineIds = new Set(onlineUsers.map((u) => u.userId));
  const [username, setUsername] = useState('');
  const [inviteRole, setInviteRole] = useState<AssignableRole>('editor');

  const handleInvite = async (event: FormEvent) => {
    event.preventDefault();
    const normalizedUsername = username.trim();
    if (!normalizedUsername || !onAddMember) return;
    if (await onAddMember(normalizedUsername, inviteRole)) {
      setUsername('');
    }
  };

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
            {canManage && member.role !== 'owner' ? (
              <div className="member-controls">
                <select
                  className="select member-role-select"
                  aria-label={`Role for ${member.username}`}
                  value={member.role}
                  disabled={actionKey !== null}
                  onChange={(event) => {
                    void onUpdateRole?.(
                      member.user_id,
                      event.target.value as AssignableRole,
                    );
                  }}
                >
                  <option value="editor">Editor</option>
                  <option value="viewer">Viewer</option>
                </select>
                <button
                  className="btn btn-sm member-remove"
                  type="button"
                  aria-label={`Remove ${member.username}`}
                  disabled={actionKey !== null}
                  onClick={() => void onRemoveMember?.(member.user_id)}
                >
                  Remove
                </button>
              </div>
            ) : (
              <span className={`badge badge-${member.role}`}>
                {member.role}
              </span>
            )}
          </div>
        );
      })}
      {canManage && (
        <form className="invite-form" onSubmit={handleInvite}>
          <label htmlFor="invite-username">Invite by username</label>
          <input
            id="invite-username"
            className="input"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="Registered username"
            minLength={3}
            maxLength={64}
            required
          />
          <div className="invite-actions">
            <select
              className="select"
              aria-label="Invitation role"
              value={inviteRole}
              onChange={(event) =>
                setInviteRole(event.target.value as AssignableRole)
              }
              disabled={actionKey !== null}
            >
              <option value="editor">Editor</option>
              <option value="viewer">Viewer</option>
            </select>
            <button
              className="btn btn-primary btn-sm"
              type="submit"
              disabled={actionKey !== null || username.trim().length < 3}
            >
              {actionKey === 'invite' ? 'Adding...' : 'Add member'}
            </button>
          </div>
        </form>
      )}
      {feedback && (
        <p
          className={`member-feedback member-feedback-${feedback.kind}`}
          role={feedback.kind === 'error' ? 'alert' : 'status'}
        >
          {feedback.message}
        </p>
      )}
    </div>
  );
}
