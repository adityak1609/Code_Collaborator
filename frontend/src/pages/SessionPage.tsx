import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { sessionsApi } from '../api/client';
import type { SessionDetail } from '../api/client';
import { useAuthStore } from '../store/authStore';
import { CollaborativeEditor } from '../components/Editor/CollaborativeEditor';
import { MemberList } from '../components/Sidebar/MemberList';
import type {
  ConnectionStatus,
  DocumentSaveStatusEvent,
  PresenceUser,
} from '../types/collaboration';

type SaveStatus = 'idle' | 'unsaved' | 'saving' | 'saved' | 'error';
type AssignableRole = 'viewer' | 'editor';
type MemberFeedback = { kind: 'success' | 'error'; message: string };

export function SessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting');
  const [onlineUsers, setOnlineUsers] = useState<PresenceUser[]>([]);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const [documentReady, setDocumentReady] = useState(false);
  const [editorEpoch, setEditorEpoch] = useState(0);
  const [authorizationRefreshing, setAuthorizationRefreshing] = useState(false);
  const [memberActionKey, setMemberActionKey] = useState<string | null>(null);
  const [memberFeedback, setMemberFeedback] = useState<MemberFeedback | null>(null);
  const getDocumentStateRef = useRef<(() => Uint8Array) | null>(null);
  const documentVersionRef = useRef(0);
  const saveRequestRef = useRef(0);
  const saveInFlightRef = useRef(false);

  // Determine current user's role
  const myRole = session?.members.find((m) => m.user_id === user?.id)?.role || 'viewer';
  const effectiveRole = authorizationRefreshing ? 'viewer' : myRole;

  const fetchSession = useCallback(async (finishInitialLoading: boolean) => {
    if (!sessionId) return;

    try {
      const res = await sessionsApi.get(sessionId);
      setSession(res.data);
      setError(null);
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
      if (finishInitialLoading) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- state changes occur after the awaited HTTP request.
    void fetchSession(true);
  }, [fetchSession]);

  const handleConnectionChange = useCallback(
    (status: ConnectionStatus) => {
      setConnectionStatus(status);
    },
    []
  );

  const handlePresenceUpdate = useCallback((users: PresenceUser[]) => {
    setOnlineUsers(users);
  }, []);

  const handleAuthorizationChange = useCallback(() => {
    // Drop the old provider/Y.Doc immediately. It may contain a local update
    // rejected after a demotion and CRDT synchronization cannot remove it.
    setAuthorizationRefreshing(true);
    setEditorEpoch((epoch) => epoch + 1);
    getDocumentStateRef.current = null;
    documentVersionRef.current = 0;
    saveRequestRef.current += 1;
    saveInFlightRef.current = false;
    setDocumentReady(false);
    setSaveStatus('idle');
    void fetchSession(false).finally(() => setAuthorizationRefreshing(false));
  }, [fetchSession]);

  const handleDocumentReady = useCallback(
    (getState: (() => Uint8Array) | null) => {
      getDocumentStateRef.current = getState;
      setDocumentReady(getState !== null);
    },
    []
  );

  const handleDocumentChange = useCallback(() => {
    documentVersionRef.current += 1;
    setSaveStatus((current) => current === 'saving' ? current : 'unsaved');
  }, []);

  const handleDocumentSaveStatus = useCallback((status: DocumentSaveStatusEvent) => {
    setLastSavedAt(status.saved_at);
    const isSaved = !status.dirty && status.matchesCurrentDocument;
    setSaveStatus(() => {
      // Let an in-flight HTTP save finish its version check before a stale or
      // incomplete status frame can replace the progress indicator.
      if (saveInFlightRef.current) return 'saving';
      return isSaved ? 'saved' : 'unsaved';
    });
  }, []);

  const handleSave = useCallback(async () => {
    if (!sessionId || effectiveRole === 'viewer' || saveInFlightRef.current) return;

    const getDocumentState = getDocumentStateRef.current;
    if (!getDocumentState) {
      setSaveStatus('error');
      return;
    }

    const versionAtSave = documentVersionRef.current;
    const requestId = ++saveRequestRef.current;
    saveInFlightRef.current = true;
    setSaveStatus('saving');
    try {
      const response = await sessionsApi.save(sessionId, getDocumentState());
      if (saveRequestRef.current !== requestId) return;
      setLastSavedAt(response.data.saved_at);
      setSaveStatus(
        !response.data.dirty && documentVersionRef.current === versionAtSave
          ? 'saved'
          : 'unsaved'
      );
    } catch {
      if (saveRequestRef.current === requestId) setSaveStatus('error');
    } finally {
      if (saveRequestRef.current === requestId) saveInFlightRef.current = false;
    }
  }, [effectiveRole, sessionId]);

  const runMemberAction = useCallback(
    async (
      actionKey: string,
      action: () => Promise<unknown>,
      successMessage: string,
    ) => {
      setMemberActionKey(actionKey);
      setMemberFeedback(null);
      try {
        await action();
        await fetchSession(false);
        setMemberFeedback({ kind: 'success', message: successMessage });
        return true;
      } catch (err: any) {
        const message = err.response?.data?.detail || 'Member update failed';
        setMemberFeedback({ kind: 'error', message });
        return false;
      } finally {
        setMemberActionKey(null);
      }
    },
    [fetchSession],
  );

  const handleAddMember = useCallback(
    async (username: string, role: AssignableRole) => {
      if (!sessionId) return false;
      return runMemberAction(
        'invite',
        () => sessionsApi.addMember(sessionId, username, role),
        `${username} added as ${role}.`,
      );
    },
    [runMemberAction, sessionId],
  );

  const handleUpdateRole = useCallback(
    async (userId: string, role: AssignableRole) => {
      if (!sessionId) return false;
      return runMemberAction(
        `role:${userId}`,
        () => sessionsApi.updateRole(sessionId, userId, role),
        `Member role changed to ${role}.`,
      );
    },
    [runMemberAction, sessionId],
  );

  const handleRemoveMember = useCallback(
    async (userId: string) => {
      if (!sessionId) return false;
      return runMemberAction(
        `remove:${userId}`,
        () => sessionsApi.removeMember(sessionId, userId),
        'Member removed.',
      );
    },
    [runMemberAction, sessionId],
  );

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void handleSave();
      }
    };
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [handleSave]);

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
          {effectiveRole !== 'viewer' && (
            <>
              <span className={`save-status save-status-${saveStatus}`}>
                {saveStatus === 'saving' && 'Saving...'}
                {saveStatus === 'saved' && (lastSavedAt
                  ? `Saved ${new Date(lastSavedAt).toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit',
                  })}`
                  : 'Saved')}
                {saveStatus === 'unsaved' && 'Unsaved changes'}
                {saveStatus === 'error' && 'Save failed'}
              </span>
              <button
                className="btn btn-primary btn-sm"
                onClick={() => void handleSave()}
                disabled={saveStatus === 'saving' || !documentReady}
                title="Save to PostgreSQL (Ctrl/Cmd+S)"
              >
                {saveStatus === 'saving' ? 'Saving...' : 'Save'}
              </button>
            </>
          )}
          <span className={`badge badge-${effectiveRole}`}>{effectiveRole}</span>
          {effectiveRole === 'viewer' && (
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
            key={`${sessionId}:${editorEpoch}`}
            sessionId={sessionId!}
            language={session.language}
            role={effectiveRole}
            onConnectionChange={handleConnectionChange}
            onPresenceUpdate={handlePresenceUpdate}
            onDocumentReady={handleDocumentReady}
            onDocumentChange={handleDocumentChange}
            onDocumentSaveStatus={handleDocumentSaveStatus}
            onAuthorizationChange={handleAuthorizationChange}
          />
        </div>

        {/* Sidebar */}
        <div className="sidebar-panel">
          <MemberList
            members={session.members}
            onlineUsers={onlineUsers}
            currentUserId={user?.id || ''}
            canManage={effectiveRole === 'owner'}
            actionKey={memberActionKey}
            feedback={memberFeedback}
            onAddMember={handleAddMember}
            onUpdateRole={handleUpdateRole}
            onRemoveMember={handleRemoveMember}
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
