export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

export interface PresenceUser {
  name: string;
  color: string;
  userId: string;
}
