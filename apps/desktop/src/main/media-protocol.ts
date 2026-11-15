import type { CustomScheme } from 'electron';

import { isUuid4 } from '../shared/api';

/**
 * The `app-media` scheme (docs/04 section 1): the renderer's `<video>` plays a
 * registered source or preview through it, and main forwards each request to the
 * sidecar. The renderer gets no filesystem path, no sidecar URL and no token.
 *
 * Only `app-media://source/<uuid4>` and `app-media://preview/<uuid4>` exist.
 * Everything else (another host, a query, a fragment, extra segments, a port or
 * credentials, a malformed id) is a 404 that never reaches the sidecar.
 */

export const MEDIA_SCHEME = 'app-media';

/**
 * Must be registered with `protocol.registerSchemesAsPrivileged` before the app
 * is ready. `stream` lets media elements read ranges. No `supportFetchAPI`: only
 * `<video>` reads the scheme, the renderer never fetches it.
 */
export const MEDIA_SCHEME_REGISTRATION: CustomScheme = {
  scheme: MEDIA_SCHEME,
  privileges: { standard: true, secure: true, stream: true },
};

export type MediaKind = 'source' | 'preview';

export interface MediaTarget {
  readonly kind: MediaKind;
  readonly id: string;
}

// Matched on the serialized URL itself, so no URL parser can read it differently.
const MEDIA_URL = /^app-media:\/\/(source|preview)\/([^/?#]+)$/;

/** The target of an `app-media` URL, or null for anything that is not exactly one. */
export function parseMediaUrl(url: string): MediaTarget | null {
  const match = MEDIA_URL.exec(url);
  const kind = match?.[1];
  const id = match?.[2];
  if ((kind !== 'source' && kind !== 'preview') || !isUuid4(id)) {
    return null;
  }
  return { kind, id };
}

/** The sidecar path of a target; both ids already passed the uuid4 check. */
export function mediaPath(target: MediaTarget): string {
  return target.kind === 'source'
    ? `/v1/sources/${target.id}/media`
    : `/v1/previews/${target.id}/media`;
}

/** What the protocol needs from the sidecar: `Sidecar.fetchMedia` and nothing else. */
export interface MediaBackend {
  fetchMedia(path: string, range: string | null, signal?: AbortSignal): Promise<Response>;
}

/** The part of Electron's `protocol` used here. */
export interface ProtocolRegistry {
  handle(scheme: string, handler: (request: Request) => Promise<Response>): void;
}

/** The handler of `app-media`: validates the URL, then streams from the sidecar. */
export function createMediaHandler(backend: MediaBackend): (request: Request) => Promise<Response> {
  return async (request) => {
    const target = parseMediaUrl(request.url);
    if (target === null) {
      return new Response(null, { status: 404 });
    }
    if (request.method !== 'GET') {
      return new Response(null, { status: 405, headers: { Allow: 'GET' } });
    }
    try {
      return await backend.fetchMedia(mediaPath(target), request.headers.get('Range'), request.signal);
    } catch {
      return new Response(null, { status: 502 });
    }
  };
}

export function registerMediaProtocol(registry: ProtocolRegistry, backend: MediaBackend): void {
  registry.handle(MEDIA_SCHEME, createMediaHandler(backend));
}
