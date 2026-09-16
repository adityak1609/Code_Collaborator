export type ExecutionStatus =
  | 'QUEUED'
  | 'STARTING'
  | 'RUNNING'
  | 'COMPLETED'
  | 'FAILED'
  | 'TIMEOUT'
  | 'CANCELLED';

export interface ExecutionRecord {
  id: string;
  session_id: string;
  triggered_by: string | null;
  status: ExecutionStatus;
  code: string;
  language: 'python' | 'cpp' | 'javascript';
  stdout: string | null;
  stderr: string | null;
  exit_code: number | null;
  elapsed_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface ExecutionHistory {
  items: ExecutionRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface ExecutionStatusEvent {
  type: 'execution_status';
  execution_id: string;
  session_id: string;
  status: ExecutionStatus;
  stdout: string | null;
  stderr: string | null;
  exit_code: number | null;
  elapsed_ms: number | null;
}

export interface ExecutionOutputEvent {
  type: 'execution_output';
  execution_id: string;
  session_id: string;
  stream: 'stdout' | 'stderr';
  data: string;
  status: 'RUNNING';
}

export type ExecutionEvent = ExecutionStatusEvent | ExecutionOutputEvent;

export const ACTIVE_EXECUTION_STATUSES = new Set<ExecutionStatus>([
  'QUEUED',
  'STARTING',
  'RUNNING',
]);

export function isExecutionEvent(value: unknown): value is ExecutionEvent {
  if (!value || typeof value !== 'object') return false;
  const event = value as Partial<ExecutionEvent>;
  if (typeof event.execution_id !== 'string' || typeof event.session_id !== 'string') {
    return false;
  }
  if (event.type === 'execution_output') {
    return (event.stream === 'stdout' || event.stream === 'stderr') &&
      typeof event.data === 'string' && event.status === 'RUNNING';
  }
  return event.type === 'execution_status' && typeof event.status === 'string';
}
