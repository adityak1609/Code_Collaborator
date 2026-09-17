const withoutTrailingSlash = (value: string) => value.replace(/\/+$/, '');

const productionWebSocketUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
};

export const API_URL = withoutTrailingSlash(
  import.meta.env.VITE_API_URL || (import.meta.env.PROD ? '/api' : 'http://localhost:8000')
);

// y-websocket appends /{roomName}, so this must include the backend's /ws prefix.
export const WS_URL = withoutTrailingSlash(
  import.meta.env.VITE_WS_URL || (
    import.meta.env.PROD ? productionWebSocketUrl() : 'ws://localhost:8000/ws'
  )
);
