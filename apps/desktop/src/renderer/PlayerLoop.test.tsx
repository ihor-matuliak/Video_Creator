import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SourcePlayer } from './SourcePlayer';
import type { PlayerHandle } from './SourcePlayer';
import { makeSource } from './test-bridge';

// «Повтор фрагмента» (docs/03 section 8): a fragment played with the loop on starts over at its
// end; with it off it pauses there. jsdom plays nothing, so the calls the player makes are checked.

/** Long enough for several checks of the end of a fragment (they run every few tens of ms). */
const SEVERAL_CHECKS_MS = 250;

let media: {
  play: ReturnType<typeof stubPlay>;
  pause: ReturnType<typeof stubPause>;
};

function stubPlay() {
  return vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
}

function stubPause() {
  return vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
}

beforeEach(() => {
  media = { play: stubPlay(), pause: stubPause() };
});

function setPlayhead(element: HTMLVideoElement, seconds: number): void {
  Object.defineProperty(element, 'currentTime', { configurable: true, writable: true, value: seconds });
}

/** A player whose video has its metadata, so that it can play fragments. */
function openPlayer(): { handle: PlayerHandle; element: HTMLVideoElement } {
  const ref = createRef<PlayerHandle>();
  render(<SourcePlayer source={makeSource()} ref={ref} />);
  const element = screen.getByTestId('player-video');
  if (!(element instanceof HTMLVideoElement)) {
    throw new TypeError('player-video is not a <video>');
  }
  setPlayhead(element, 0);
  fireEvent.loadedMetadata(element);
  const handle = ref.current;
  if (handle === null) {
    throw new Error('the player gave no handle');
  }
  return { handle, element };
}

async function letChecksRun(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => {
      window.setTimeout(resolve, SEVERAL_CHECKS_MS);
    });
  });
}

function toggleLoop(): void {
  fireEvent.click(screen.getByTestId('player-loop'));
}

describe('Повтор фрагмента', () => {
  it('is a toggle button that starts off', () => {
    openPlayer();

    const button = screen.getByTestId('player-loop');
    expect(button).toHaveTextContent('Повтор фрагмента');
    expect(button).toHaveAttribute('aria-pressed', 'false');
    toggleLoop();
    expect(button).toHaveAttribute('aria-pressed', 'true');
    toggleLoop();
    expect(button).toHaveAttribute('aria-pressed', 'false');
  });

  it('on: goes back to the start and plays on every time the end is reached', async () => {
    const { handle, element } = openPlayer();
    toggleLoop();

    act(() => {
      handle.playRange(4120, 7880);
    });
    expect(element.currentTime).toBe(4.12);
    expect(media.play).toHaveBeenCalledTimes(1);

    setPlayhead(element, 7.9);
    await waitFor(() => {
      expect(media.play).toHaveBeenCalledTimes(2);
    });
    expect(element.currentTime).toBe(4.12);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:04.120');

    // And once more: the pass that started over is looped as well.
    setPlayhead(element, 8);
    await waitFor(() => {
      expect(media.play).toHaveBeenCalledTimes(3);
    });
    expect(element.currentTime).toBe(4.12);
    expect(media.pause).not.toHaveBeenCalled();
  });

  it('on: a fragment that ends where the source ends loops when the video ends', () => {
    const { handle, element } = openPlayer();
    toggleLoop();

    act(() => {
      handle.playRange(8000, 12_000);
    });
    expect(media.play).toHaveBeenCalledTimes(1);

    // The video reports its end before (or instead of) the check of the end is run.
    setPlayhead(element, 12);
    fireEvent.ended(element);

    expect(element.currentTime).toBe(8);
    expect(media.play).toHaveBeenCalledTimes(2);
    expect(media.pause).not.toHaveBeenCalled();
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:08.000');
  });

  it('off: pauses once at the end and plays no further', async () => {
    const { handle, element } = openPlayer();

    act(() => {
      handle.playRange(4120, 7880);
    });
    setPlayhead(element, 7.9);
    await waitFor(() => {
      expect(media.pause).toHaveBeenCalledTimes(1);
    });
    expect(element.currentTime).toBe(7.88);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:07.880');

    await letChecksRun();
    expect(media.play).toHaveBeenCalledTimes(1);
    expect(media.pause).toHaveBeenCalledTimes(1);
  });

  it('off: a fragment that ends where the source ends just ends', () => {
    const { handle, element } = openPlayer();

    act(() => {
      handle.playRange(8000, 12_000);
    });
    setPlayhead(element, 12);
    fireEvent.ended(element);

    expect(element.currentTime).toBe(12);
    expect(media.play).toHaveBeenCalledTimes(1);
  });

  it('switched off during a pass: that pass stops at the end', async () => {
    const { handle, element } = openPlayer();
    toggleLoop();
    act(() => {
      handle.playRange(4120, 7880);
    });

    toggleLoop();
    setPlayhead(element, 7.9);

    await waitFor(() => {
      expect(media.pause).toHaveBeenCalledTimes(1);
    });
    expect(element.currentTime).toBe(7.88);
    await letChecksRun();
    expect(media.play).toHaveBeenCalledTimes(1);
  });
});

describe('Повтор фрагмента: the fragment is dropped', () => {
  async function expectNoLoop(element: HTMLVideoElement, playhead: number): Promise<void> {
    setPlayhead(element, playhead);
    await letChecksRun();
    expect(media.play).toHaveBeenCalledTimes(1);
    expect(media.pause).not.toHaveBeenCalled();
    expect(element.currentTime).toBe(playhead);
  }

  it('by dragging the position slider', async () => {
    const { handle, element } = openPlayer();
    toggleLoop();
    act(() => {
      handle.playRange(4120, 7880);
    });

    fireEvent.change(screen.getByTestId('player-seek'), { target: { value: '9000' } });

    expect(element.currentTime).toBe(9);
    expect(screen.getByTestId('player-time')).toHaveTextContent('00:00:09.000');
    await expectNoLoop(element, 9.5);
  });

  it('by seekTo', async () => {
    const { handle, element } = openPlayer();
    toggleLoop();
    act(() => {
      handle.playRange(4120, 7880);
    });

    act(() => {
      handle.seekTo(9000);
    });

    expect(element.currentTime).toBe(9);
    await expectNoLoop(element, 9.5);
  });

  it('by seekBy', async () => {
    const { handle, element } = openPlayer();
    toggleLoop();
    act(() => {
      handle.playRange(4120, 7880);
    });
    setPlayhead(element, 5);

    act(() => {
      handle.seekBy(4000);
    });

    expect(element.currentTime).toBe(9);
    await expectNoLoop(element, 9.5);
  });
});
