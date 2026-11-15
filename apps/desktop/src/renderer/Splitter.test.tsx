import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Splitter } from './Splitter';
import type { SplitterProps } from './Splitter';

function setup(overrides: Partial<SplitterProps> = {}) {
  const onResize = vi.fn<(px: number) => void>();
  const onCommit = vi.fn<(px: number) => void>();
  const props: SplitterProps = {
    panel: 'list',
    label: 'Ширина списку фрагментів',
    controls: 'list-pane',
    testId: 'splitter-list',
    value: 320,
    max: 480,
    getStart: () => 320,
    getMax: () => 480,
    onResize,
    onCommit,
    ...overrides,
  };
  const view = render(<Splitter {...props} />);
  return { splitter: within(view.container).getByRole('separator'), onResize, onCommit, view };
}

describe('Splitter semantics', () => {
  it('is a focusable vertical separator with a name, a value and its limits', () => {
    const { splitter } = setup();

    expect(splitter).toHaveAccessibleName('Ширина списку фрагментів');
    expect(splitter).toHaveAttribute('aria-orientation', 'vertical');
    expect(splitter).toHaveAttribute('aria-valuenow', '320');
    expect(splitter).toHaveAttribute('aria-valuemin', '240');
    expect(splitter).toHaveAttribute('aria-valuemax', '480');
    expect(splitter).toHaveAttribute('aria-valuetext', '320 px');
    expect(splitter).toHaveAttribute('aria-controls', 'list-pane');
    expect(splitter).toHaveAttribute('tabindex', '0');
    splitter.focus();
    expect(splitter).toHaveFocus();
  });

  it('takes the limits of the right panel for the right panel', () => {
    const { splitter } = setup({ panel: 'details', value: 300, label: 'Ширина панелі фрагмента' });

    expect(splitter).toHaveAttribute('aria-valuemin', '280');
    expect(splitter).toHaveAttribute('aria-valuemax', '480');
  });

  it('reports the room that is left as its maximum, never above its limit or under its minimum', () => {
    const { view } = setup({ max: 350 });
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuemax', '350');

    view.rerender(
      <Splitter
        panel="list"
        label="Ширина списку фрагментів"
        controls="list-pane"
        testId="splitter-list"
        value={240}
        max={100}
        getStart={() => 240}
        getMax={() => 100}
        onResize={() => undefined}
        onCommit={() => undefined}
      />,
    );
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuemax', '240');

    view.rerender(
      <Splitter
        panel="list"
        label="Ширина списку фрагментів"
        controls="list-pane"
        testId="splitter-list"
        value={320}
        max={9999}
        getStart={() => 320}
        getMax={() => 9999}
        onResize={() => undefined}
        onCommit={() => undefined}
      />,
    );
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuemax', '480');
  });
});

describe('Splitter keyboard', () => {
  it('moves the list with the arrow keys and remembers every press', () => {
    const { splitter, onCommit } = setup();

    fireEvent.keyDown(splitter, { key: 'ArrowRight' });
    fireEvent.keyDown(splitter, { key: 'ArrowLeft' });

    expect(onCommit.mock.calls).toEqual([[336], [304]]);
  });

  it('moves the right panel against the arrow: left makes it wider', () => {
    const { splitter, onCommit } = setup({ panel: 'details' });

    fireEvent.keyDown(splitter, { key: 'ArrowLeft' });
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });

    expect(onCommit.mock.calls).toEqual([[336], [304]]);
  });

  it('takes a bigger step with Shift and goes to the ends with Home and End', () => {
    const { splitter, onCommit } = setup({ getMax: () => 400 });

    fireEvent.keyDown(splitter, { key: 'ArrowRight', shiftKey: true });
    fireEvent.keyDown(splitter, { key: 'Home' });
    fireEvent.keyDown(splitter, { key: 'End' });

    expect(onCommit.mock.calls).toEqual([[384], [240], [400]]);
  });

  it('stops at the limits and at the room that is left', () => {
    const { splitter, onCommit } = setup({ getStart: () => 478, getMax: () => 480 });
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });

    const narrow = setup({ getStart: () => 245, getMax: () => 480 });
    fireEvent.keyDown(narrow.splitter, { key: 'ArrowLeft' });

    const crowded = setup({ getStart: () => 340, getMax: () => 345 });
    fireEvent.keyDown(crowded.splitter, { key: 'ArrowRight' });

    expect(onCommit.mock.calls).toEqual([[480]]);
    expect(narrow.onCommit.mock.calls).toEqual([[240]]);
    expect(crowded.onCommit.mock.calls).toEqual([[345]]);
  });

  it('cancels the browser action of a key it used, and of no other', () => {
    const { splitter, onCommit, onResize } = setup();

    expect(fireEvent.keyDown(splitter, { key: 'ArrowRight' })).toBe(false);
    expect(fireEvent.keyDown(splitter, { key: 'ArrowDown' })).toBe(true);
    expect(fireEvent.keyDown(splitter, { key: 'a' })).toBe(true);
    expect(fireEvent.keyDown(splitter, { key: 'Tab' })).toBe(true);

    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onResize).not.toHaveBeenCalled();
  });

  it('leaves a shortcut with Ctrl, Alt or Meta alone', () => {
    const { splitter, onCommit } = setup();

    fireEvent.keyDown(splitter, { key: 'ArrowRight', ctrlKey: true });
    fireEvent.keyDown(splitter, { key: 'ArrowRight', altKey: true });
    fireEvent.keyDown(splitter, { key: 'ArrowRight', metaKey: true });

    expect(onCommit).not.toHaveBeenCalled();
  });

  it('moves a held key at once but remembers it only when the key is let go', () => {
    const { splitter, onResize, onCommit } = setup();

    fireEvent.keyDown(splitter, { key: 'ArrowRight', repeat: true });
    fireEvent.keyDown(splitter, { key: 'ArrowRight', repeat: true });
    expect(onResize.mock.calls).toEqual([[336], [336]]);
    expect(onCommit).not.toHaveBeenCalled();

    fireEvent.keyUp(splitter, { key: 'ArrowRight' });
    expect(onCommit.mock.calls).toEqual([[336]]);

    fireEvent.keyUp(splitter, { key: 'ArrowRight' });
    expect(onCommit).toHaveBeenCalledTimes(1);
  });

  it('remembers a held key when the focus leaves before it is let go', () => {
    const { splitter, onCommit } = setup();

    fireEvent.keyDown(splitter, { key: 'ArrowRight', repeat: true });
    fireEvent.blur(splitter);

    expect(onCommit.mock.calls).toEqual([[336]]);
  });
});

describe('Splitter pointer', () => {
  it('follows the pointer while it moves and remembers the width when it is released', () => {
    const { splitter, onResize, onCommit } = setup();

    fireEvent.pointerDown(splitter, { button: 0, clientX: 400, pointerId: 1 });
    expect(splitter).toHaveAttribute('data-dragging', 'true');
    expect(splitter).toHaveFocus();
    fireEvent.pointerMove(window, { clientX: 430, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 460, pointerId: 1 });

    expect(onResize.mock.calls).toEqual([[350], [380]]);
    expect(onCommit).not.toHaveBeenCalled();

    fireEvent.pointerUp(window, { clientX: 460, pointerId: 1 });

    expect(onCommit.mock.calls).toEqual([[380]]);
    expect(splitter).toHaveAttribute('data-dragging', 'false');
  });

  it('drags the right panel the other way round', () => {
    const { splitter, onResize, onCommit } = setup({ panel: 'details', getStart: () => 320 });

    fireEvent.pointerDown(splitter, { button: 0, clientX: 900, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 850, pointerId: 1 });
    fireEvent.pointerUp(window, { clientX: 850, pointerId: 1 });

    expect(onResize.mock.calls).toEqual([[370]]);
    expect(onCommit.mock.calls).toEqual([[370]]);
  });

  it('is clamped to the limits and to the room that was left when the drag began', () => {
    const { splitter, onResize } = setup({ getMax: () => 400 });

    fireEvent.pointerDown(splitter, { button: 0, clientX: 400, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 2000, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: -2000, pointerId: 1 });

    expect(onResize.mock.calls).toEqual([[400], [240]]);
  });

  it('remembers nothing for a click that did not move', () => {
    const { splitter, onResize, onCommit } = setup();

    fireEvent.pointerDown(splitter, { button: 0, clientX: 400, pointerId: 1 });
    fireEvent.pointerUp(window, { clientX: 400, pointerId: 1 });

    expect(onResize).not.toHaveBeenCalled();
    expect(onCommit).not.toHaveBeenCalled();
    expect(splitter).toHaveAttribute('data-dragging', 'false');
  });

  it('ends the gesture on a cancelled pointer and ignores the pointer afterwards', () => {
    const { splitter, onResize, onCommit } = setup();

    fireEvent.pointerDown(splitter, { button: 0, clientX: 400, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 420, pointerId: 1 });
    fireEvent.pointerCancel(window, { pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 500, pointerId: 1 });

    expect(onResize.mock.calls).toEqual([[340]]);
    expect(onCommit.mock.calls).toEqual([[340]]);
  });

  it('ignores a button that is not the primary one', () => {
    const { splitter, onResize } = setup();

    fireEvent.pointerDown(splitter, { button: 2, clientX: 400, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 450, pointerId: 1 });

    expect(onResize).not.toHaveBeenCalled();
    expect(splitter).toHaveAttribute('data-dragging', 'false');
  });

  it('stops listening to the window when it is removed in the middle of a drag', () => {
    const { splitter, onResize, onCommit, view } = setup();

    fireEvent.pointerDown(splitter, { button: 0, clientX: 400, pointerId: 1 });
    view.unmount();
    fireEvent.pointerMove(window, { clientX: 450, pointerId: 1 });
    fireEvent.pointerUp(window, { clientX: 450, pointerId: 1 });

    expect(onResize).not.toHaveBeenCalled();
    expect(onCommit).not.toHaveBeenCalled();
  });
});
