import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { sessionsApi } from '../api/client';
import type { Session } from '../api/client';
import { useAuthStore } from '../store/authStore';

export function DashboardPage() {
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [newName, setNewName] = useState('');
  const [newLang, setNewLang] = useState('python');
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let isActive = true;

    sessionsApi.list()
      .then((res) => {
        if (isActive) setSessions(res.data);
      })
      .catch((err) => {
        if (isActive) console.error('Failed to fetch sessions', err);
      })
      .finally(() => {
        if (isActive) setLoading(false);
      });

    return () => {
      isActive = false;
    };
  }, []);

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true);
    try {
      const res = await sessionsApi.create(newName, newLang);
      setShowModal(false);
      setNewName('');
      navigate(`/session/${res.data.id}`);
    } catch (err) {
      console.error('Failed to create session', err);
    } finally {
      setCreating(false);
    }
  };

  const langLabel: Record<string, string> = {
    python: 'Python',
    cpp: 'C++',
    javascript: 'JavaScript',
  };

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <h1>Concord</h1>
        <div className="flex items-center gap-md">
          <span style={{ color: 'var(--text-secondary)', fontSize: 14 }}>
            {user?.username}
          </span>
          <button className="btn btn-sm" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <div className="dashboard-content">
        <div className="flex justify-between items-center" style={{ marginBottom: 20 }}>
          <h2 style={{ fontSize: 18, fontWeight: 600 }}>Your Sessions</h2>
          <button className="btn btn-primary" onClick={() => setShowModal(true)}>
            + New Session
          </button>
        </div>

        {loading ? (
          <p style={{ color: 'var(--text-secondary)' }}>Loading sessions…</p>
        ) : sessions.length === 0 ? (
          <div className="card" style={{ textAlign: 'center', padding: 40 }}>
            <p style={{ color: 'var(--text-secondary)', marginBottom: 16 }}>
              No sessions yet. Create one to get started.
            </p>
            <button className="btn btn-primary" onClick={() => setShowModal(true)}>
              Create your first session
            </button>
          </div>
        ) : (
          <div className="sessions-grid">
            {sessions.map((session) => (
              <div
                key={session.id}
                className="card card-hover session-card"
                onClick={() => navigate(`/session/${session.id}`)}
              >
                <h3>{session.name}</h3>
                <div className="meta">
                  <span className={`badge badge-${session.language}`}>
                    {langLabel[session.language] || session.language}
                  </span>
                  <span>
                    Created {new Date(session.created_at).toLocaleDateString()}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Create Session Modal */}
      {showModal && (
        <div className="modal-overlay" onClick={() => setShowModal(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>New Session</h2>
            <form onSubmit={handleCreate}>
              <div className="form-group" style={{ marginBottom: 16 }}>
                <label htmlFor="session-name">Session Name</label>
                <input
                  id="session-name"
                  className="input"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="e.g. Algorithm Practice"
                  required
                  autoFocus
                />
              </div>
              <div className="form-group">
                <label htmlFor="session-language">Language</label>
                <select
                  id="session-language"
                  className="select"
                  value={newLang}
                  onChange={(e) => setNewLang(e.target.value)}
                >
                  <option value="python">Python</option>
                  <option value="cpp">C++</option>
                  <option value="javascript">JavaScript</option>
                </select>
              </div>
              <div className="modal-actions">
                <button
                  type="button"
                  className="btn"
                  onClick={() => setShowModal(false)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={creating}
                >
                  {creating ? 'Creating…' : 'Create'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
