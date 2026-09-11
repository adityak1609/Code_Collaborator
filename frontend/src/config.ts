const withoutTrailingSlash = (value: string) => value.replace(/\/+$/, '');

export const API_URL = withoutTrailingSlash(
  import.meta.env.VITE_API_URL || 'http://localhost:8000'
);

// y-websocket appends /{roomName}, so this must include the backend's /ws prefix.
export const WS_URL = withoutTrailingSlash(
  import.meta.env.VITE_WS_URL || 'ws://localhost:8000/ws'
);
