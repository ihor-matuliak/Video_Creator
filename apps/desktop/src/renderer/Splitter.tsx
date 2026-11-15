import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';

import { dragWidth, keyboardWidth, limitsOf } from './layout-logic';
import type { PanelId } from './layout-logic';

export interface SplitterProps {
  /** The panel this splitter resizes: the list grows to the right, the right panel to the left. */
  readonly panel: PanelId;
  /** Accessible name, e.g. «Ширина списку фрагментів». */
  readonly label: string;
  /** `id` of the panel it controls. */
  readonly controls: string;
  readonly testId: string;
  /** The width on screen now (aria-valuenow). */
  readonly value: number;
  /** The widest the panel can be now (aria-valuemax); never above its limit. */
  readonly max: number;
  /** The width on screen at the moment a gesture starts. */
  readonly getStart: () => number;
  /** The widest the panel can be at the moment a gesture starts. */
  readonly getMax: () => number;
  /** The width changes while the gesture goes on; nothing is remembered yet. */
  readonly onResize: (px: number) => void;
  /** The gesture ended (pointer released, key pressed): remember `px`. */
  readonly onCommit: (px: number) => void;
}

/**
 * A window splitter (WAI-ARIA `separator` that can be focused): it is dragged with the pointer
 * and moved with the arrow keys (Shift for bigger steps, Home and End for the ends).
 */
export function Splitter({
  panel,
  label,
  controls,
  testId,
  value,
  max,
  getStart,
  getMax,
  onResize,
  onCommit,
}: SplitterProps) {
  const limits = limitsOf(panel);
  const [dragging, setDragging] = useState(false);
  const gesture = useRef<AbortController | null>(null);
  // A held key moves the panel at once but is remembered when it is let go, not 30 times a second.
  const pendingKey = useRef<number | null>(null);

  useEffect(
    () => () => {
      gesture.current?.abort();
    },
    [],
  );

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) {
      return;
    }
    // No text selection while dragging; the splitter itself takes the focus.
    event.preventDefault();
    event.currentTarget.focus();
    gesture.current?.abort();
    const controller = new AbortController();
    gesture.current = controller;
    const startX = event.clientX;
    const startWidth = getStart();
    const widest = getMax();
    let latest = startWidth;
    let moved = false;
    setDragging(true);

    const finish = (): void => {
      controller.abort();
      gesture.current = null;
      setDragging(false);
      if (moved) {
        onCommit(latest);
      }
    };
    window.addEventListener(
      'pointermove',
      (move: PointerEvent) => {
        latest = dragWidth(panel, startWidth, startX, move.clientX, widest);
        moved = true;
        onResize(latest);
      },
      { signal: controller.signal },
    );
    window.addEventListener('pointerup', finish, { signal: controller.signal });
    window.addEventListener('pointercancel', finish, { signal: controller.signal });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    const next = keyboardWidth(panel, event.key, event.shiftKey, getStart(), getMax());
    if (next === null) {
      return;
    }
    event.preventDefault();
    if (event.repeat) {
      pendingKey.current = next;
      onResize(next);
    } else {
      pendingKey.current = null;
      onCommit(next);
    }
  };

  const handleKeyUp = (): void => {
    if (pendingKey.current !== null) {
      const pending = pendingKey.current;
      pendingKey.current = null;
      onCommit(pending);
    }
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-controls={controls}
      aria-valuenow={value}
      aria-valuemin={limits.min}
      aria-valuemax={Math.max(limits.min, Math.min(limits.max, max))}
      aria-valuetext={`${String(value)} px`}
      tabIndex={0}
      className="splitter"
      data-testid={testId}
      data-dragging={dragging}
      title="Потягніть або натисніть ← і →, щоб змінити ширину. Shift — більший крок, Home і End — межі"
      onPointerDown={handlePointerDown}
      onKeyDown={handleKeyDown}
      onKeyUp={handleKeyUp}
      onBlur={handleKeyUp}
    />
  );
}
