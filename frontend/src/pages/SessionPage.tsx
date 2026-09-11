import { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { sessionsApi } from '../api/client';
import type { SessionDetail } from '../api/client';
import { useAuthStore } from '../store/authStore';
import { CollaborativeEditor } from '../components/Editor/CollaborativeEditor';
import { MemberList } from '../components/Sidebar/MemberList';
import type { ConnectionStatus, PresenceUser } from '../types/collaboration';

export function SessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting');
  const [onlineUsers, setOnlineUsers] = useState<PresenceUser[]>([]);

  // Determine current user's role
  const myRole = session?.members.find((m) => m.user_id === user?.id)?.role || 'viewer';

  useEffect(() => {
    if (!sessionId) return;

    const fetchSession = async () => {
      try {
        const res = await sessionsApi.get(sessionId);
        setSession(res.data);
      } catch (err: any) {
        if (err.response?.status === 403) {
          setError('You do not have access to this session.');
        } else if (err.response?.status === 404) {
          setError('Session not found.');
        } else if (err.response?.status === 410) {
          setError('This session has been closed.');
        } else {
          setError('Failed to load session.');
        }
      } finally {
        setLoading(false);
      }
    };

    fetchSession();
  }, [sessionId]);

  const handleConnectionChange = useCallback(
    (status: ConnectionStatus) => {
      setConnectionStatus(status);
    },
    []
  );

  const handlePresenceUpdate = useCallback((users: PresenceUser[]) => {
    setOnlineUsers(users);
  }, []);

  if (loading) {
    return (
      <div className="workspace">
        <div className="flex justify-center items-center flex-1">
          <p style={{ color: 'var(--text-secondary)' }}>Loading session…</p>
        </div>
      </div>
    );
  }

  if (error || !session) {
    return (
      <div className="workspace">
        <div className="flex flex-col justify-center items-center flex-1 gap-md">
          <p style={{ color: 'var(--accent-red)' }}>{error || 'Session not found'}</p>
          <button className="btn" onClick={() => navigate('/')}>
            Back to Dashboard
          </button>
        </div>
      </div>
    );
  }

  const langLabel: Record<string, string> = {
    python: 'Python',
    cpp: 'C++',
    javascript: 'JavaScript',
  };

  return (
    <div className="workspace">
      {/* Header */}
      <header className="workspace-header">
        <div className="flex items-center gap-md">
          <button
            className="btn btn-sm"
            onClick={() => navigate('/')}
            title="Back to dashboard"
          >
            ← Back
          </button>
          <span className="session-name">{session.name}</span>
          <span className={`badge badge-${session.language}`}>
            {langLabel[session.language] || session.language}
          </span>
        </div>
        <div className="flex items-center gap-sm">
          <span className={`badge badge-${myRole}`}>{myRole}</span>
          {myRole === 'viewer' && (
            <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>
              Read-only
            </span>
          )}
        </div>
      </header>

      {/* Body */}
      <div className="workspace-body">
        {/* Editor panel */}
        <div className="editor-panel">
          <CollaborativeEditor
            sessionId={sessionId!}
            language={session.language}
            role={myRole}
            onConnectionChange={handleConnectionChange}
            onPresenceUpdate={handlePresenceUpdate}
          />
        </div>

        {/* Sidebar */}
        <div className="sidebar-panel">
          <MemberList
            members={session.members}
            onlineUsers={onlineUsers}
            currentUserId={user?.id || ''}
          />
        </div>
      </div>

      {/* Status bar */}
      <div className="status-bar">
        <div className="status-indicator">
          <span className={`dot ${connectionStatus}`} />
          <span>
            {connectionStatus === 'connected'
              ? 'Connected'
              : connectionStatus === 'connecting'
              ? 'Connecting…'
              : 'Disconnected'}
          </span>
        </div>
        <span>
          {onlineUsers.length} user{onlineUsers.length === 1 ? '' : 's'} online
        </span>
      </div>
    </div>
  );
}
