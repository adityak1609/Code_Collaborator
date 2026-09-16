import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { ExecutionRecord } from '../../types/execution';
import { ACTIVE_EXECUTION_STATUSES } from '../../types/execution';

interface Props {
  executions: ExecutionRecord[];
  selected: ExecutionRecord | null;
  loading: boolean;
  error: string | null;
  collapsed: boolean;
  canCancel: boolean;
  onSelect: (id: string) => void;
  onCancel: () => void;
  onToggle: () => void;
}

function shortTime(value: string) {
  return new Date(value).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

const ANSI_COLORS: Record<number, string> = {
  30: '#64748b', 31: '#ff6b81', 32: '#36d399', 33: '#f7c948',
  34: '#7c8cff', 35: '#c084fc', 36: '#22d3ee', 37: '#e2e8f0',
  90: '#94a3b8', 91: '#fb7185', 92: '#6ee7b7', 93: '#fde68a',
  94: '#a5b4fc', 95: '#d8b4fe', 96: '#67e8f9', 97: '#f8fafc',
};

function renderAnsi(text: string, prefix: string, fallback?: string): ReactNode[] {
  // oxlint-disable-next-line no-control-regex -- ANSI SGR begins with ESC.
  const pattern = /\u001b\[([0-9;]*)m/g;
  const nodes: ReactNode[] = [];
  let style: CSSProperties = fallback ? { color: fallback } : {};
  let offset = 0;
  let index = 0;
  for (const match of text.matchAll(pattern)) {
    const position = match.index;
    if (position > offset) {
      nodes.push(<span style={style} key={`${prefix}-${index++}`}>{text.slice(offset, position)}</span>);
    }
    const codes = (match[1] || '0').split(';').map(Number);
    for (const code of codes) {
      if (code === 0) style = fallback ? { color: fallback } : {};
      else if (code === 1) style = { ...style, fontWeight: 700 };
      else if (code === 22) style = { ...style, fontWeight: 400 };
      else if (code === 39) style = { ...style, color: fallback };
      else if (ANSI_COLORS[code]) style = { ...style, color: ANSI_COLORS[code] };
    }
    offset = position + match[0].length;
  }
  if (offset < text.length) {
    nodes.push(<span style={style} key={`${prefix}-${index}`}>{text.slice(offset)}</span>);
  }
  return nodes;
}

export function OutputPanel({
  executions,
  selected,
  loading,
  error,
  collapsed,
  canCancel,
  onSelect,
  onCancel,
  onToggle,
}: Props) {
  const active = selected && ACTIVE_EXECUTION_STATUSES.has(selected.status);
  const consoleRef = useRef<HTMLDivElement>(null);
  const [followOutput, setFollowOutput] = useState(true);

  useEffect(() => {
    if (!followOutput || !consoleRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      if (consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [followOutput, selected?.stdout, selected?.stderr, selected?.status]);

  return (
    <section className={`terminal-panel ${collapsed ? 'terminal-collapsed' : ''}`}>
      <div className="terminal-header">
        <div className="terminal-title-group">
          <span className="terminal-prompt" aria-hidden="true">›_</span>
          <strong>Output</strong>
          {selected && (
            <span className={`execution-status execution-status-${selected.status.toLowerCase()}`}>
              {selected.status}
            </span>
          )}
        </div>
        <div className="terminal-actions">
          {active && canCancel && (
            <button className="btn btn-danger btn-sm" onClick={onCancel}>
              Stop
            </button>
          )}
          <button
            className="terminal-toggle"
            onClick={onToggle}
            aria-label={collapsed ? 'Expand output' : 'Collapse output'}
          >
            {collapsed ? '⌃' : '⌄'}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="terminal-body">
          <nav className="run-history" aria-label="Execution history">
            <div className="run-history-label">Recent runs</div>
            {loading && executions.length === 0 && <p className="terminal-muted">Loading…</p>}
            {!loading && executions.length === 0 && (
              <p className="terminal-muted">No runs yet</p>
            )}
            {executions.map((execution) => (
              <button
                key={execution.id}
                className={`run-history-item ${selected?.id === execution.id ? 'active' : ''}`}
                onClick={() => {
                  setFollowOutput(true);
                  onSelect(execution.id);
                }}
              >
                <span className={`run-dot run-dot-${execution.status.toLowerCase()}`} />
                <span>
                  <strong>{execution.status.toLowerCase()}</strong>
                  <small>{shortTime(execution.created_at)}</small>
                </span>
              </button>
            ))}
          </nav>

          <div
            className="terminal-console"
            aria-live="polite"
            ref={consoleRef}
            onScroll={(event) => {
              const target = event.currentTarget;
              setFollowOutput(
                target.scrollHeight - target.scrollTop - target.clientHeight < 24,
              );
            }}
          >
            {error && <div className="terminal-error" role="alert">{error}</div>}
            {!selected && !error && (
              <div className="terminal-empty">
                <span className="terminal-empty-icon">▶</span>
                <p>Run the current collaborative draft to see output here.</p>
                <small>Ctrl/Cmd + Enter</small>
              </div>
            )}
            {selected && (
              <>
                <div className="terminal-meta">
                  <span>{selected.language}</span>
                  {selected.elapsed_ms !== null && <span>{selected.elapsed_ms} ms</span>}
                  {selected.exit_code !== null && <span>exit {selected.exit_code}</span>}
                  {active && <span className="terminal-live">live</span>}
                </div>
                <pre className="terminal-output">
                  {renderAnsi(selected.stdout || '', 'stdout')}
                  {selected.stderr && (
                    <span className="stderr-output">
                      {renderAnsi(selected.stderr, 'stderr', '#ff7b8b')}
                    </span>
                  )}
                  {active && <span className="terminal-cursor" />}
                  {!active && !selected.stdout && !selected.stderr && '[process exited without output]'}
                </pre>
                {!followOutput && (
                  <button
                    className="terminal-follow"
                    onClick={() => setFollowOutput(true)}
                  >
                    Jump to latest ↓
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
