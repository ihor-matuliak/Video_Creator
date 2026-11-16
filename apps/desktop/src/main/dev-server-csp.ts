import type { Plugin } from 'vite';

const CSP_META = /(<meta\s+http-equiv="Content-Security-Policy"\s+content=")([^"]*)(")/;
const BASE_DIRECTIVE = "default-src 'self'";

// Directives added on top of the strict policy. Vite dev server injects an
// inline React Refresh preamble and inline <style> tags, and talks HMR over a
// websocket; the strict policy forbids all three.
const RELAXED_DIRECTIVES: readonly (readonly [name: string, value: string])[] = [
  ['script-src', "'self' 'unsafe-inline'"],
  ['style-src', "'self' 'unsafe-inline'"],
  ['connect-src', "'self' ws://localhost:* ws://127.0.0.1:*"],
];

/**
 * Builds the dev-server policy from the strict policy declared in index.html.
 * Every strict directive (for example `media-src app-media:`) is kept as is.
 * Throws when the policy is not the shape this transform understands, so a
 * changed index.html can never silently leave the strict policy in `serve`.
 */
export function relaxContentSecurityPolicy(html: string): string {
  const match = CSP_META.exec(html);
  if (match === null) {
    throw new Error(
      'dev-server-csp: index.html has no <meta http-equiv="Content-Security-Policy" content="..."> tag to relax.',
    );
  }

  const policy = match[2] ?? '';
  const directives = policy
    .split(';')
    .map((directive) => directive.trim())
    .filter((directive) => directive !== '');

  if (!directives.includes(BASE_DIRECTIVE)) {
    throw new Error(
      `dev-server-csp: expected the strict policy in index.html to contain "${BASE_DIRECTIVE}", found "${policy}". ` +
        'Update src/main/dev-server-csp.ts together with index.html.',
    );
  }

  const added = new Set(RELAXED_DIRECTIVES.map(([name]) => name));
  const clashing = directives.filter((directive) =>
    added.has((directive.split(/\s+/)[0] ?? '').toLowerCase()),
  );
  if (clashing.length > 0) {
    throw new Error(
      `dev-server-csp: index.html already declares ${clashing.join('; ')}; the dev transform only adds ` +
        'script-src, style-src and connect-src, so it cannot relax them safely. ' +
        'Update src/main/dev-server-csp.ts together with index.html.',
    );
  }

  const relaxed = [
    ...directives,
    ...RELAXED_DIRECTIVES.map(([name, value]) => `${name} ${value}`),
  ].join('; ');

  // A replacer function keeps `$&` and similar sequences in the policy literal.
  return html.replace(
    CSP_META,
    (_match, open: string, _policy: string, close: string) => `${open}${relaxed}${close}`,
  );
}

// Applies only to `electron-vite dev` (serve); the built renderer keeps the
// strict policy from index.html unchanged.
export function devServerCsp(): Plugin {
  return {
    name: 'clipforge-dev-server-csp',
    apply: 'serve',
    transformIndexHtml(html) {
      return relaxContentSecurityPolicy(html);
    },
  };
}
