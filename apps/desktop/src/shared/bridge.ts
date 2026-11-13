/**
 * Typed surface that the preload script exposes to the renderer as
 * `window.clipforge`. The renderer never gets Node, the filesystem, the session
 * token or a raw HTTP client; allowlisted IPC methods are added here as
 * features need them.
 */

/** IPC channel of the backend status query (ipcMain.handle in main). */
export const APP_GET_STATUS_CHANNEL = 'app:get-status';

export type AppState = 'starting' | 'ready' | 'failed';
export type AppMode = 'production' | 'test';

export interface AppStatus {
  readonly state: AppState;
  readonly mode: AppMode;
  /** Backend version reported by /v1/health; null until the backend is ready. */
  readonly version: string | null;
}

export interface ClipforgeBridge {
  readonly bridgeVersion: 1;
  readonly getStatus: () => Promise<AppStatus>;
}

function isAppState(value: unknown): value is AppState {
  return value === 'starting' || value === 'ready' || value === 'failed';
}

function isAppMode(value: unknown): value is AppMode {
  return value === 'production' || value === 'test';
}

/** Validates an IPC payload; the renderer must not trust unchecked data. */
export function parseAppStatus(value: unknown): AppStatus {
  if (typeof value === 'object' && value !== null) {
    const { state, mode, version } = value as Record<string, unknown>;
    if (isAppState(state) && isAppMode(mode) && (version === null || typeof version === 'string')) {
      return { state, mode, version };
    }
  }
  throw new TypeError('Некоректна відповідь app:get-status');
}
