import * as decoding from 'lib0/decoding';
import * as Y from 'yjs';
import { WebsocketProvider } from 'y-websocket';

const API = (process.env.CONCORD_API_URL || 'http://localhost:8000').replace(/\/$/, '');
const WS = (process.env.CONCORD_WS_URL || 'ws://localhost:8000/ws').replace(/\/$/, '');
const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
const password = 'Concord-execution-9!';
const MESSAGE_EXECUTION_EVENT = 5;
const MESSAGE_DOCUMENT_STATUS = 4;

async function request(path, { method = 'GET', token, body } = {}) {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body && !(body instanceof Uint8Array) ? { 'Content-Type': 'application/json' } : {}),
      ...(body instanceof Uint8Array ? { 'Content-Type': 'application/octet-stream' } : {}),
    },
    ...(body ? { body: body instanceof Uint8Array ? body : JSON.stringify(body) } : {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`${method} ${path} failed (${response.status}): ${JSON.stringify(payload)}`);
  }
  return payload;
}

async function waitFor(check, label, timeout = 300_000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const value = await check();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function saveCode(token, sessionId, code) {
  const doc = new Y.Doc();
  doc.getText('monaco').insert(0, code);
  await request(`/sessions/${sessionId}/save`, {
    method: 'POST',
    token,
    body: Y.encodeStateAsUpdate(doc),
  });
  doc.destroy();
}

async function createRun(token, language, code) {
  const session = await request('/sessions', {
    method: 'POST', token, body: { name: `${language} execution ${suffix}`, language },
  });
  await saveCode(token, session.id, code);
  const execution = await request(`/sessions/${session.id}/run`, { method: 'POST', token });
  return { session, execution };
}

async function terminal(token, sessionId, executionId, statuses) {
  return waitFor(async () => {
    const item = await request(`/sessions/${sessionId}/executions/${executionId}`, { token });
    return statuses.includes(item.status) ? item : null;
  }, `${statuses.join('/')} execution`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const sessions = [];
let provider;
let liveDoc;
let token;

try {
  const username = `runner_${suffix}`;
  await request('/auth/register', {
    method: 'POST',
    body: { username, email: `${username}@example.com`, password },
  });
  ({ access_token: token } = await request('/auth/login', {
    method: 'POST', body: { username, password },
  }));

  const liveSession = await request('/sessions', {
    method: 'POST', token, body: { name: `Live output ${suffix}`, language: 'python' },
  });
  sessions.push(liveSession.id);
  await saveCode(token, liveSession.id,
    'import time\nprint("live-first", flush=True)\ntime.sleep(1)\nprint("live-second", flush=True)');
  liveDoc = new Y.Doc();
  const events = [];
  provider = new WebsocketProvider(WS, liveSession.id, liveDoc, {
    params: { token }, disableBc: true, maxBackoffTime: 1_000, connect: false,
  });
  provider.messageHandlers[MESSAGE_DOCUMENT_STATUS] = (_encoder, decoder) => {
    decoding.readVarString(decoder);
  };
  provider.messageHandlers[MESSAGE_EXECUTION_EVENT] = (_encoder, decoder) => {
    events.push(JSON.parse(decoding.readVarString(decoder)));
  };
  provider.connect();
  await waitFor(() => provider.wsconnected && provider.synced, 'WebSocket synchronization');
  const liveExecution = await request(`/sessions/${liveSession.id}/run`, {
    method: 'POST', token,
  });
  await waitFor(() => events.some((event) =>
    event.type === 'execution_output' && event.status === 'RUNNING' &&
    event.data.includes('live-first')),
  'incremental stdout event', 15_000).catch((error) => {
    throw new Error(`${error.message}; received ${JSON.stringify(events)}`);
  });
  const liveResult = await terminal(token, liveSession.id, liveExecution.id, ['COMPLETED']);
  assert(liveResult.stdout === 'live-first\nlive-second\n', 'Python stdout was incorrect');

  const javascript = await createRun(token, 'javascript',
    'console.log("javascript-ok");');
  sessions.push(javascript.session.id);
  const jsResult = await terminal(token, javascript.session.id, javascript.execution.id, ['COMPLETED']);
  assert(jsResult.stdout === 'javascript-ok\n', 'JavaScript stdout was incorrect');

  const cpp = await createRun(token, 'cpp',
    '#include <iostream>\nint main(){ std::cout << "cpp-ok\\n"; }');
  sessions.push(cpp.session.id);
  const cppResult = await terminal(token, cpp.session.id, cpp.execution.id, ['COMPLETED']);
  assert(cppResult.stdout === 'cpp-ok\n', 'C++ stdout was incorrect');

  const compileError = await createRun(token, 'cpp', 'int main( { return 0; }');
  sessions.push(compileError.session.id);
  const compileResult = await terminal(token, compileError.session.id,
    compileError.execution.id, ['FAILED']);
  assert(compileResult.exit_code !== 0 && compileResult.stderr,
    'C++ compile error was not captured');

  const cancellation = await createRun(token, 'python',
    'import time\nwhile True: time.sleep(0.1)');
  sessions.push(cancellation.session.id);
  await terminal(token, cancellation.session.id, cancellation.execution.id, ['RUNNING']);
  await request(`/sessions/${cancellation.session.id}/executions/${cancellation.execution.id}/cancel`, {
    method: 'POST', token,
  });
  await terminal(token, cancellation.session.id, cancellation.execution.id, ['CANCELLED']);

  const timeout = await createRun(token, 'python', 'while True: pass');
  sessions.push(timeout.session.id);
  await terminal(token, timeout.session.id, timeout.execution.id, ['TIMEOUT']);

  const network = await createRun(token, 'python', [
    'import socket',
    'try:',
    '    socket.create_connection(("example.com", 80), 1)',
    'except OSError:',
    '    print("network-blocked")',
    'else:',
    '    raise SystemExit("network-open")',
  ].join('\n'));
  sessions.push(network.session.id);
  const networkResult = await terminal(token, network.session.id, network.execution.id,
    ['COMPLETED', 'FAILED']);
  assert(networkResult.status === 'COMPLETED' && networkResult.stdout === 'network-blocked\n',
    'Sandbox network isolation failed');

  const memory = await createRun(token, 'python',
    'data = bytearray(320 * 1024 * 1024)\nprint(len(data))');
  sessions.push(memory.session.id);
  const memoryResult = await terminal(token, memory.session.id, memory.execution.id, ['FAILED']);
  assert(memoryResult.exit_code !== 0, 'Sandbox memory limit was not enforced');

  console.log(JSON.stringify({
    status: 'passed',
    checks: ['live-output', 'python', 'javascript', 'cpp', 'compile-error',
      'cancellation', 'timeout', 'network-isolation', 'memory-limit'],
  }));
} finally {
  provider?.destroy();
  liveDoc?.destroy();
  if (token) {
    for (const sessionId of sessions) {
      await request(`/sessions/${sessionId}`, { method: 'DELETE', token }).catch(() => undefined);
    }
  }
}
