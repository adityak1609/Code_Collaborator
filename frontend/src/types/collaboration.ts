export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

export interface PresenceUser {
  name: string;
  color: string;
  userId: string;
}

export interface DocumentSavedEvent {
  type: 'document_saved';
  saved_at: string | null;
  state_vector: string | null;
  state_hash: string;
  dirty: boolean;
  saved_by: string | null;
}

export interface DocumentSaveStatusEvent extends DocumentSavedEvent {
  matchesCurrentDocument: boolean;
}
