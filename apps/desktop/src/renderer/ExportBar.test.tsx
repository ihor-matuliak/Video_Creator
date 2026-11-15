import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ExportBar } from './ExportBar';
import { FOLDER_TOKEN, PROJECT_ID, installBridge, makeClip, makeSource } from './test-bridge';

// docs/03 section 12: the sticky bar must not hide a control that is scrolled into view. jsdom does
// no layout, so the real check (elementFromPoint at every size and scaling) is e2e/layout.spec.mjs;
// here: the scrolling area is told to keep the bar's height clear, and lets go when the bar is not docked.

const PROPERTY = 'scroll-padding-bottom';
const SELECTION = { selected: 1, approved: [makeClip({ review_status: 'approved' })], skipped: [] };

/** A ResizeObserver that tells nothing by itself: the test calls it. */
class StubObserver {
  static readonly all: StubObserver[] = [];
  disconnected = false;
  constructor(readonly callback: () => void) {
    StubObserver.all.push(this);
  }
  observe(): void {
    // The test decides when the size changed.
  }
  unobserve(): void {
    // Not used.
  }
  disconnect(): void {
    this.disconnected = true;
  }
}

function setBarHeight(height: number): void {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
    () => new DOMRect(0, 0, 300, height),
  );
}

function renderBar(): { readonly main: HTMLElement; readonly unmount: () => void } {
  installBridge({ chooseExportFolder: () => ({ token: FOLDER_TOKEN, displayName: 'Нарізка' }) });
  const { container, unmount } = render(
    <main>
      <ExportBar
        projectId={PROJECT_ID}
        sources={[makeSource()]}
        selection={SELECTION}
        onCreated={() => undefined}
        onReviewSkipped={() => undefined}
      />
    </main>,
  );
  const main = container.querySelector('main');
  if (main === null) {
    throw new Error('main is missing');
  }
  return { main, unmount };
}

afterEach(() => {
  StubObserver.all.length = 0;
  vi.unstubAllGlobals();
});

describe('ExportBar and the scrolling area', () => {
  it('keeps the bar height and a little air clear, follows the bar when it grows', () => {
    vi.stubGlobal('ResizeObserver', StubObserver);
    setBarHeight(100.2);

    const { main } = renderBar();

    expect(main.style.getPropertyValue(PROPERTY)).toBe('109px');
    expect(screen.getByTestId('export-bar')).toHaveAttribute('data-docked', 'true');

    // A skipped-clips list makes the bar taller: the observer tells, the room follows.
    setBarHeight(180);
    act(() => {
      for (const observer of StubObserver.all) {
        observer.callback();
      }
    });

    expect(main.style.getPropertyValue(PROPERTY)).toBe('188px');
  });

  it('lets go of the room while the summary is open (the bar is then in the flow), and when it goes away', async () => {
    vi.stubGlobal('ResizeObserver', StubObserver);
    setBarHeight(100);
    const { main } = renderBar();
    expect(main.style.getPropertyValue(PROPERTY)).toBe('108px');

    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');

    expect(screen.getByTestId('export-bar')).toHaveAttribute('data-docked', 'false');
    expect(main.style.getPropertyValue(PROPERTY)).toBe('');

    fireEvent.click(screen.getByTestId('export-summary-cancel'));

    expect(screen.getByTestId('export-bar')).toHaveAttribute('data-docked', 'true');
    expect(main.style.getPropertyValue(PROPERTY)).toBe('108px');
  });

  it('removes the room and stops observing when the bar is gone', () => {
    vi.stubGlobal('ResizeObserver', StubObserver);
    setBarHeight(100);
    const { main, unmount } = renderBar();
    const observers = [...StubObserver.all];
    expect(observers.length).toBeGreaterThan(0);
    expect(main.style.getPropertyValue(PROPERTY)).toBe('108px');

    unmount();

    expect(observers.every((observer) => observer.disconnected)).toBe(true);
    expect(main.style.getPropertyValue(PROPERTY)).toBe('');
  });

  it('works without ResizeObserver (jsdom): the room is set once', () => {
    setBarHeight(100);

    const { main } = renderBar();

    expect(main.style.getPropertyValue(PROPERTY)).toBe('108px');
  });
});
