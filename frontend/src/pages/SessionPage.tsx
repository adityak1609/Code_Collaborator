import { useEffect, useState, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { executionsApi, sessionsApi } from '../api/client';
import type { SessionDetail } from '../api/client';
import { useAuthStore } from '../store/authStore';
import { CollaborativeEditor } from '../components/Editor/CollaborativeEditor';
import { MemberList } from '../components/Sidebar/MemberList';
import { SnapshotPanel } from '../components/Sidebar/SnapshotPanel';
import { OutputPanel } from '../components/Terminal/OutputPanel';
import type {
  ConnectionStatus,
  DocumentSaveStatusEvent,
  PresenceUser,
} from '../types/collaboration';
import type { ExecutionEvent, ExecutionRecord } from '../types/execution';
import { ACTIVE_EXECUTION_STATUSES } from '../types/execution';

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
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
  const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(null);
  const [executionLoading, setExecutionLoading] = useState(true);
  const [executionError, setExecutionError] = useState<string | null>(null);
  const [terminalCollapsed, setTerminalCollapsed] = useState(false);
  const getDocumentStateRef = useRef<(() => Uint8Array) | null>(null);
  const documentVersionRef = useRef(0);
  const saveRequestRef = useRef(0);
  const saveInFlightRef = useRef(false);
  const executionIdsRef = useRef(new Set<string>());
  const selectedExecution = executions.find(
    (execution) => execution.id === selectedExecutionId,
  ) || null;

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

  const upsertExecution = useCallback((next: ExecutionRecord) => {
    executionIdsRef.current.add(next.id);
    setExecutions((current) => {
      const exists = current.some((item) => item.id === next.id);
      const merged = exists
        ? current.map((item) => item.id === next.id ? {
          ...next,
          stdout: next.stdout ?? item.stdout,
          stderr: next.stderr ?? item.stderr,
        } : item)
        : [next, ...current];
      return merged
        .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
        .slice(0, 20);
    });
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    let active = true;
    executionsApi.list(sessionId)
      .then((response) => {
        if (!active) return;
        executionIdsRef.current = new Set(response.data.items.map((item) => item.id));
        setExecutions(response.data.items);
        setSelectedExecutionId((current) => current || response.data.items[0]?.id || null);
        setExecutionError(null);
      })
      .catch(() => {
        if (active) setExecutionError('Execution history is unavailable.');
      })
      .finally(() => {
        if (active) setExecutionLoading(false);
      });
    return () => { active = false; };
  }, [sessionId]);

  const handleExecutionEvent = useCallback((event: ExecutionEvent) => {
    const isKnown = executionIdsRef.current.has(event.execution_id);
    setExecutions((current) => current.map((execution) => {
      if (execution.id !== event.execution_id) return execution;
      if (event.type === 'execution_output') {
        return {
          ...execution,
          [event.stream]: `${execution[event.stream] || ''}${event.data}`,
        };
      }
      return {
        ...execution,
        status: event.status,
        stdout: event.stdout ?? execution.stdout,
        stderr: event.stderr ?? execution.stderr,
        exit_code: event.exit_code,
        elapsed_ms: event.elapsed_ms,
        finished_at: ACTIVE_EXECUTION_STATUSES.has(event.status)
          ? execution.finished_at
          : execution.finished_at || new Date().toISOString(),
      };
    }));
    setSelectedExecutionId(event.execution_id);
    setTerminalCollapsed(false);
    setExecutionError(null);
    if (!isKnown && sessionId) {
      void executionsApi.get(sessionId, event.execution_id)
        .then((response) => upsertExecution(response.data));
    }
  }, [sessionId, upsertExecution]);

  const handleRun = useCallback(async () => {
    if (!sessionId || effectiveRole === 'viewer') return;
    setExecutionError(null);
    setTerminalCollapsed(false);
    try {
      const response = await executionsApi.run(sessionId);
      upsertExecution(response.data);
      setSelectedExecutionId(response.data.id);
    } catch (err: any) {
      setExecutionError(err.response?.data?.detail || 'Unable to start execution.');
    }
  }, [effectiveRole, sessionId, upsertExecution]);

  const handleCancel = useCallback(async () => {
    if (!sessionId || !selectedExecution) return;
    try {
      const response = await executionsApi.cancel(sessionId, selectedExecution.id);
      upsertExecution(response.data);
    } catch (err: any) {
      setExecutionError(err.response?.data?.detail || 'Unable to stop execution.');
    }
  }, [selectedExecution, sessionId, upsertExecution]);

  useEffect(() => {
    if (!sessionId || !selectedExecution ||
        !ACTIVE_EXECUTION_STATUSES.has(selectedExecution.status)) return;
    const interval = window.setInterval(() => {
      void executionsApi.get(sessionId, selectedExecution.id)
        .then((response) => upsertExecution(response.data))
        .catch(() => undefined);
    }, 1000);
    return () => window.clearInterval(interval);
  }, [selectedExecution, sessionId, upsertExecution]);

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

  const handleSnapshotRestored = useCallback(() => {
    documentVersionRef.current += 1;
    setSaveStatus('unsaved');
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
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        void handleRun();
      }
    };
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [handleRun, handleSave]);

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
        <div className="workspace-identity">
          <button
            className="icon-button"
            onClick={() => navigate('/')}
            title="Back to dashboard"
            aria-label="Back to dashboard"
          >
            ←
          </button>
          <span className="brand-mark" aria-hidden="true">C</span>
          <div className="workspace-title-stack">
            <span className="session-name">{session.name}</span>
            <span className="workspace-subtitle">Live collaborative workspace</span>
          </div>
          <span className={`badge badge-${session.language}`}>
            {langLabel[session.language] || session.language}
          </span>
        </div>
        <div className="workspace-actions">
          {effectiveRole !== 'viewer' && (
            <>
              <button
                className="btn btn-run"
                onClick={() => void handleRun()}
                disabled={!documentReady}
                title="Run current draft (Ctrl/Cmd+Enter)"
              >
                <span aria-hidden="true">▶</span> Run
              </button>
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
                className="btn btn-save"
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
        <main className="workspace-main">
          <div className="editor-tabbar">
            <div className="editor-tab active">
              <span className={`language-icon language-icon-${session.language}`}>
                {session.language === 'python' ? 'Py' : session.language === 'cpp' ? 'C+' : 'JS'}
              </span>
              main.{session.language === 'python' ? 'py' : session.language === 'cpp' ? 'cpp' : 'js'}
              {saveStatus === 'unsaved' && <span className="unsaved-dot" title="Unsaved changes" />}
            </div>
            <span className="editor-tab-hint">Shared draft · changes sync instantly</span>
          </div>
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
              onExecutionEvent={handleExecutionEvent}
              onAuthorizationChange={handleAuthorizationChange}
            />
          </div>
          <OutputPanel
            executions={executions}
            selected={selectedExecution}
            loading={executionLoading}
            error={executionError}
            collapsed={terminalCollapsed}
            canCancel={effectiveRole !== 'viewer'}
            onSelect={setSelectedExecutionId}
            onCancel={() => void handleCancel()}
            onToggle={() => setTerminalCollapsed((value) => !value)}
          />
        </main>

        {/* Sidebar */}
        <div className="sidebar-panel">
          <SnapshotPanel
            sessionId={sessionId!}
            canEdit={effectiveRole !== 'viewer'}
            onRestored={handleSnapshotRestored}
          />
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
