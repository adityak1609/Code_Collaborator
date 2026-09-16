import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ExecutionRecord } from '../../types/execution';
import { OutputPanel } from './OutputPanel';

const execution: ExecutionRecord = {
  id: 'execution-id',
  session_id: 'session-id',
  triggered_by: 'user-id',
  status: 'RUNNING',
  code: 'print("ok")',
  language: 'python',
  stdout: '\u001b[32mgreen\u001b[0m\n',
  stderr: 'warning\n',
  exit_code: null,
  elapsed_ms: null,
  created_at: '2026-09-16T08:00:00Z',
  finished_at: null,
};

describe('OutputPanel', () => {
  it('renders ANSI output without exposing control sequences', () => {
    const { container } = render(
      <OutputPanel
        executions={[execution]}
        selected={execution}
        loading={false}
        error={null}
        collapsed={false}
        canCancel
        onSelect={vi.fn()}
        onCancel={vi.fn()}
        onToggle={vi.fn()}
      />,
    );

    expect(screen.getByText('green')).toHaveStyle({ color: '#36d399' });
    expect(screen.getByText('warning')).toBeInTheDocument();
    expect(container.textContent).not.toContain('\u001b');
  });

  it('offers cancellation only for an active execution', () => {
    const cancel = vi.fn();
    render(
      <OutputPanel
        executions={[execution]}
        selected={execution}
        loading={false}
        error={null}
        collapsed={false}
        canCancel
        onSelect={vi.fn()}
        onCancel={cancel}
        onToggle={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(cancel).toHaveBeenCalledOnce();
  });
});
