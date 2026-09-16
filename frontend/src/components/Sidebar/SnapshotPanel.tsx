import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { snapshotsApi } from '../../api/client';
import type { Snapshot } from '../../api/client';

interface Props {
  sessionId: string;
  canEdit: boolean;
  onRestored?: (snapshot: Snapshot) => void;
}

const formatBytes = (value: number) => {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

export function SnapshotPanel({ sessionId, canEdit, onRestored }: Props) {
  const [items, setItems] = useState<Snapshot[]>([]);
  const [label, setLabel] = useState('');
  const [loading, setLoading] = useState(true);
  const [actionId, setActionId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSnapshots = useCallback(async () => {
    try {
      const response = await snapshotsApi.list(sessionId);
      setItems(response.data.items);
      setError(null);
    } catch {
      setError('Snapshots are unavailable.');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- state changes occur after the awaited HTTP request.
    void loadSnapshots();
  }, [loadSnapshots]);

  const createSnapshot = async (event: FormEvent) => {
    event.preventDefault();
    const normalized = label.trim();
    if (!normalized || actionId) return;
    setActionId('create');
    setFeedback(null);
    setError(null);
    try {
      const response = await snapshotsApi.create(sessionId, normalized);
      setItems((current) => [
        response.data,
        ...current.filter((item) => item.id !== response.data.id),
      ]);
      setLabel('');
      setFeedback(`Created ${response.data.label}.`);
    } catch (requestError: any) {
      setError(requestError.response?.data?.detail || 'Could not create snapshot.');
    } finally {
      setActionId(null);
    }
  };

  const restoreSnapshot = async (snapshot: Snapshot) => {
    if (actionId || !window.confirm(
      `Restore ${snapshot.label}? Current unsaved text will be replaced.`,
    )) return;
    setActionId(snapshot.id);
    setFeedback(null);
    setError(null);
    try {
      const response = await snapshotsApi.restore(sessionId, snapshot.id);
      setFeedback(`Restored ${response.data.snapshot.label}. Save to make it permanent.`);
      onRestored?.(response.data.snapshot);
    } catch (requestError: any) {
      setError(requestError.response?.data?.detail || 'Could not restore snapshot.');
    } finally {
      setActionId(null);
    }
  };

  return (
    <section className="sidebar-section snapshot-panel" aria-labelledby="snapshot-title">
      <div className="snapshot-heading">
        <h3 id="snapshot-title">Snapshots</h3>
        {!loading && <span>{items.length}</span>}
      </div>
      <p className="snapshot-intro">Named restore points for the shared draft.</p>
      {canEdit && (
        <form className="snapshot-form" onSubmit={(event) => void createSnapshot(event)}>
          <input
            className="input"
            aria-label="Snapshot label"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="Before refactor"
            maxLength={255}
            required
          />
          <button
            className="btn btn-primary btn-sm"
            type="submit"
            disabled={!label.trim() || actionId !== null}
          >
            {actionId === 'create' ? 'Creating' : 'Create'}
          </button>
        </form>
      )}
      <div className="snapshot-list">
        {loading && <p className="snapshot-empty">Loading snapshots</p>}
        {!loading && items.length === 0 && (
          <p className="snapshot-empty">No restore points yet.</p>
        )}
        {items.map((snapshot) => (
          <article className="snapshot-item" key={snapshot.id}>
            <div className="snapshot-item-copy">
              <strong title={snapshot.label}>{snapshot.label}</strong>
              <span>
                {new Date(snapshot.created_at).toLocaleString([], {
                  month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                })} · {formatBytes(snapshot.size_bytes)}
              </span>
            </div>
            {canEdit && (
              <button
                className="btn btn-sm snapshot-restore"
                type="button"
                disabled={actionId !== null}
                onClick={() => void restoreSnapshot(snapshot)}
              >
                {actionId === snapshot.id ? 'Restoring' : 'Restore'}
              </button>
            )}
          </article>
        ))}
      </div>
      {feedback && <p className="snapshot-feedback" role="status">{feedback}</p>}
      {error && <p className="snapshot-feedback snapshot-error" role="alert">{error}</p>}
    </section>
  );
}
