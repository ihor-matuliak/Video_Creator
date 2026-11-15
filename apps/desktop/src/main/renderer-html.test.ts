import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const html = readFileSync(resolve(import.meta.dirname, '../renderer/index.html'), 'utf8');

describe('renderer index.html', () => {
  it('declares the strict default-src self content security policy', () => {
    expect(html).toContain(
      '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; media-src app-media:" />',
    );
  });

  it('allows media only from the app-media scheme', () => {
    const policy = /http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(html)?.[1] ?? '';
    const directives = policy.split(';').map((directive) => directive.trim());

    expect(directives).toContain('media-src app-media:');
    expect(directives.filter((directive) => directive.startsWith('media-src'))).toHaveLength(1);
  });

  it('is a Ukrainian document titled ClipForge Local', () => {
    expect(html).toContain('<html lang="uk">');
    expect(html).toContain('<title>ClipForge Local</title>');
  });
});
