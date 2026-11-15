import { randomBytes } from 'node:crypto';
import { isAbsolute } from 'node:path';

import type { ApiErrorInfo, AppMode } from '../shared/bridge';

/** Fixed argv of the sidecar (ADR 013). No path, token or user text goes here. */
export const SIDECAR_ARGS: readonly string[] = [
  '-m',
  'clipforge.server',
  '--port',
  '0',
  '--config-stdin',
];

const ENV_PREFIX = 'CLIPFORGE_';
const ENV_SESSION_TOKEN = 'CLIPFORGE_SESSION_TOKEN';
const MAX_VERSION_LENGTH = 64;

/** ADR 014: test mode only with CLIPFORGE_E2E=1 or CLIPFORGE_MODE=test. */
export function resolveMode(env: NodeJS.ProcessEnv): AppMode {
  return env.CLIPFORGE_E2E === '1' || env.CLIPFORGE_MODE === 'test' ? 'test' : 'production';
}

/**
 * Test mode never falls back to the real user data: CLIPFORGE_DATA_DIR must be
 * an absolute path. Production always uses the Electron userData directory.
 */
export function resolveDataDir(
  mode: AppMode,
  env: NodeJS.ProcessEnv,
  userDataDir: string,
): string | null {
  if (mode === 'production') {
    return userDataDir;
  }
  const dir = env.CLIPFORGE_DATA_DIR;
  return dir !== undefined && isAbsolute(dir) ? dir : null;
}

/** 32 random bytes as 64 hex characters (the backend requires >= 32 ASCII). */
export function createSessionToken(): string {
  return randomBytes(32).toString('hex');
}

/** The single stdin config line; the token travels only through this pipe. */
export function buildConfigLine(config: {
  readonly mode: AppMode;
  readonly dataDir: string;
  readonly token: string;
}): string {
  return `${JSON.stringify({
    mode: config.mode,
    data_dir: config.dataDir,
    session_token: config.token,
  })}\n`;
}

/**
 * Environment of the child. The session token is never inherited from the
 * environment. Production drops every CLIPFORGE_* variable (the backend would
 * reject test switches); test mode keeps them for fake-provider URLs.
 */
export function buildChildEnv(mode: AppMode, env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(env)) {
    const upper = name.toUpperCase();
    if (upper === ENV_SESSION_TOKEN || (mode === 'production' && upper.startsWith(ENV_PREFIX))) {
      continue;
    }
    result[name] = value;
  }
  return result;
}

/** Parses `{"event":"ready","port":N}`; anything else yields null. */
export function parseReadyLine(line: string): number | null {
  let document: unknown;
  try {
    document = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof document !== 'object' || document === null) {
    return null;
  }
  const { event, port } = document as Record<string, unknown>;
  if (event === 'ready' && typeof port === 'number' && Number.isInteger(port)) {
    return port >= 1 && port <= 65535 ? port : null;
  }
  return null;
}

/** Returns the backend version when /v1/health is healthy and in the expected mode. */
export function parseHealthBody(body: unknown, expectedMode: AppMode): string | null {
  if (typeof body !== 'object' || body === null) {
    return null;
  }
  const { status, mode, version } = body as Record<string, unknown>;
  if (
    status === 'ok' &&
    mode === expectedMode &&
    typeof version === 'string' &&
    version.length > 0 &&
    version.length <= MAX_VERSION_LENGTH
  ) {
    return version;
  }
  return null;
}

export type ApiMethod = 'GET' | 'POST' | 'PATCH';

const LOOPBACK = '127.0.0.1';
const MAX_ERROR_CODE_LENGTH = 100;
const MAX_ERROR_MESSAGE_LENGTH = 1_000;

/**
 * URL of an API path on the sidecar, or null when the path is not strictly
 * under /v1/. The origin is always the loopback address and the sidecar port.
 */
export function buildApiUrl(port: number, path: string): string | null {
  if (!path.startsWith('/v1/') || path.includes('\\')) {
    return null;
  }
  const origin = `http://${LOOPBACK}:${String(port)}`;
  try {
    const url = new URL(path, origin);
    return url.origin === origin && url.pathname.startsWith('/v1/') && url.hash === ''
      ? url.href
      : null;
  } catch {
    return null;
  }
}

/**
 * Reduces a backend error answer (docs/04 section 4) to code, message and
 * retryable. `details` is dropped; a body of another shape gets a generic error.
 */
export function parseApiErrorBody(body: unknown, status: number): ApiErrorInfo {
  if (typeof body === 'object' && body !== null && 'error' in body) {
    const error: unknown = body.error;
    if (typeof error === 'object' && error !== null) {
      const { code, message, retryable } = error as Record<string, unknown>;
      if (
        typeof code === 'string' &&
        code !== '' &&
        typeof message === 'string' &&
        typeof retryable === 'boolean'
      ) {
        return {
          code: code.slice(0, MAX_ERROR_CODE_LENGTH),
          message: message.slice(0, MAX_ERROR_MESSAGE_LENGTH),
          retryable,
        };
      }
    }
  }
  return {
    code: 'BACKEND_ERROR',
    message: 'Локальний рушій повернув помилку',
    retryable: status >= 500,
  };
}

/** Removes the session token from text before it reaches a log. */
export function redact(text: string, token: string): string {
  return token === '' ? text : text.split(token).join('[redacted]');
}
