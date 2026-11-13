// @vitest-environment node
import { describe, expect, it } from 'vitest';

import { isOwnPageUrl, isTrustedSender } from './ipc-guard';
import type { IpcSender, TrustedPage } from './ipc-guard';

const PAGE = 'file:///C:/app/out/renderer/index.html';
const TRUSTED: TrustedPage = { webContentsId: 7, pageUrl: PAGE };
const OWN: IpcSender = { webContentsId: 7, frameUrl: PAGE, isMainFrame: true };

describe('isOwnPageUrl', () => {
  it('accepts the same file page, ignoring query and hash', () => {
    expect(isOwnPageUrl(PAGE, PAGE)).toBe(true);
    expect(isOwnPageUrl(`${PAGE}?x=1#top`, PAGE)).toBe(true);
  });

  it('rejects another file, another protocol and garbage', () => {
    expect(isOwnPageUrl('file:///C:/other/index.html', PAGE)).toBe(false);
    expect(isOwnPageUrl('https://example.com/index.html', PAGE)).toBe(false);
    expect(isOwnPageUrl('about:blank', PAGE)).toBe(false);
    expect(isOwnPageUrl('not a url', PAGE)).toBe(false);
    expect(isOwnPageUrl('', PAGE)).toBe(false);
  });

  it('compares a dev server page by origin and path', () => {
    const dev = 'http://localhost:5173/';
    expect(isOwnPageUrl('http://localhost:5173/', dev)).toBe(true);
    expect(isOwnPageUrl('http://localhost:5173/?x=1', dev)).toBe(true);
    expect(isOwnPageUrl('http://localhost:5174/', dev)).toBe(false);
    expect(isOwnPageUrl('http://localhost:5173/other', dev)).toBe(false);
    expect(isOwnPageUrl('http://evil.example:5173/', dev)).toBe(false);
  });
});

describe('isTrustedSender', () => {
  it('accepts the top frame of the app window on its own page', () => {
    expect(isTrustedSender(OWN, TRUSTED)).toBe(true);
  });

  it('rejects when no window is trusted', () => {
    expect(isTrustedSender(OWN, null)).toBe(false);
  });

  it('rejects another web contents', () => {
    expect(isTrustedSender({ ...OWN, webContentsId: 8 }, TRUSTED)).toBe(false);
  });

  it('rejects sub frames and destroyed frames', () => {
    expect(isTrustedSender({ ...OWN, isMainFrame: false }, TRUSTED)).toBe(false);
    expect(isTrustedSender({ ...OWN, frameUrl: null }, TRUSTED)).toBe(false);
  });

  it('rejects a foreign page inside the app window', () => {
    expect(isTrustedSender({ ...OWN, frameUrl: 'https://example.com/' }, TRUSTED)).toBe(false);
  });
});
