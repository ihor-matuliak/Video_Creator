import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

import { devServerCsp, relaxContentSecurityPolicy } from './dev-server-csp';

const html = readFileSync(resolve(import.meta.dirname, '../renderer/index.html'), 'utf8');

function policyOf(document: string): string {
  return /http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(document)?.[1] ?? '';
}

function directivesOf(document: string): string[] {
  return policyOf(document)
    .split(';')
    .map((directive) => directive.trim());
}

function withPolicy(policy: string): string {
  return `<html><head><meta http-equiv="Content-Security-Policy" content="${policy}" /></head></html>`;
}

describe('dev server CSP transform', () => {
  it('relaxes the real index.html for style and script injected by Vite and keeps media-src', () => {
    const relaxed = relaxContentSecurityPolicy(html);
    const directives = directivesOf(relaxed);

    expect(policyOf(relaxed)).toContain("style-src 'self' 'unsafe-inline'");
    expect(policyOf(relaxed)).toContain('media-src app-media:');
    expect(directives).toContain("default-src 'self'");
    expect(directives).toContain("script-src 'self' 'unsafe-inline'");
    expect(directives).toContain("style-src 'self' 'unsafe-inline'");
    expect(directives).toContain("connect-src 'self' ws://localhost:* ws://127.0.0.1:*");
    expect(directives.filter((directive) => directive.startsWith('media-src'))).toHaveLength(1);
    expect(policyOf(relaxed)).toBe("default-src 'self'; media-src app-media:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://localhost:* ws://127.0.0.1:*");
  });

  it('changes nothing in index.html outside the policy', () => {
    const relaxed = relaxContentSecurityPolicy(html);

    expect(relaxed.replace(policyOf(relaxed), policyOf(html))).toBe(html);
  });

  it('runs through the serve-only plugin hook', () => {
    const plugin = devServerCsp();
    const hook = plugin.transformIndexHtml;

    expect(plugin.apply).toBe('serve');
    expect(typeof hook).toBe('function');
    if (typeof hook !== 'function') {
      return;
    }
    const result = hook.call(undefined as never, html, { path: '/index.html', filename: 'index.html' });

    expect(result).toBe(relaxContentSecurityPolicy(html));
  });

  it('throws on an unknown policy instead of leaving the strict one in place', () => {
    expect(() => relaxContentSecurityPolicy(withPolicy("default-src 'none'"))).toThrow(
      /expected the strict policy/,
    );
    expect(() => relaxContentSecurityPolicy(withPolicy('media-src app-media:'))).toThrow(
      /expected the strict policy/,
    );
  });

  it('throws when index.html has no CSP meta tag', () => {
    expect(() => relaxContentSecurityPolicy('<html><head></head></html>')).toThrow(
      /no <meta http-equiv="Content-Security-Policy"/,
    );
  });

  it('throws when index.html already declares a directive the transform would add', () => {
    expect(() =>
      relaxContentSecurityPolicy(withPolicy("default-src 'self'; style-src 'self'")),
    ).toThrow(/already declares style-src 'self'/);
  });
});
