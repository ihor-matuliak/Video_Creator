import { useLayoutEffect, useState, useSyncExternalStore } from 'react';
import type { CSSProperties } from 'react';

import type { PanelLayout } from '../shared/bridge';
import { useLayoutControl } from './layout-context';
import { WIDE_QUERY, limitsOf, maxWidthFor, widthOf, withWidth } from './layout-logic';
import type { PanelId } from './layout-logic';

function hasMatchMedia(): boolean {
  return typeof window.matchMedia === 'function';
}

function subscribeWide(onChange: () => void): () => void {
  if (!hasMatchMedia()) {
    return () => undefined;
  }
  const query = window.matchMedia(WIDE_QUERY);
  query.addEventListener('change', onChange);
  return () => {
    query.removeEventListener('change', onChange);
  };
}

function readWide(): boolean {
  // No media queries (a test DOM): the three-column arrangement, which has no tabs to open.
  return hasMatchMedia() ? window.matchMedia(WIDE_QUERY).matches : true;
}

/** True from 1440 px of window width: the right panel is a column; below, tabs under the player. */
export function useWideLayout(): boolean {
  return useSyncExternalStore(subscribeWide, readWide, () => true);
}

/** What the browser actually laid out, in whole px; 0 where nothing was measured. */
interface Measured {
  readonly container: number;
  readonly list: number;
  readonly details: number;
}

const UNMEASURED: Measured = { container: 0, list: 0, details: 0 };

/** The props one splitter needs; spread them and add `label`, `controls` and `testId`. */
export interface PanelControl {
  readonly panel: PanelId;
  readonly value: number;
  readonly max: number;
  readonly getStart: () => number;
  readonly getMax: () => number;
  readonly onResize: (px: number) => void;
  readonly onCommit: (px: number) => void;
}

export interface MomentsPanels {
  /** Callback refs for the three elements the layout measures: the grid, the list and the right panel. */
  readonly attachGrid: (element: HTMLDivElement | null) => void;
  readonly attachList: (element: HTMLElement | null) => void;
  readonly attachDetails: (element: HTMLElement | null) => void;
  /** CSS variables with the widths the grid asks for; the grid itself keeps the player at its minimum. */
  readonly style: CSSProperties;
  readonly control: (panel: PanelId) => PanelControl;
}

function whole(element: Element | null): number {
  return element === null ? 0 : Math.round(element.getBoundingClientRect().width);
}

/**
 * The widths of the list and of the right panel. They start as the remembered ones, follow a
 * splitter while it moves, and are handed back (`commit`) when the user lets go. The widths are
 * what is asked of the grid; what the grid gives (a narrow window squeezes the panels, never the
 * player below its minimum) is measured for `aria-valuenow`.
 */
export function useMomentsPanels(wide: boolean): MomentsPanels {
  const { layout, commit } = useLayoutControl();
  const [widths, setWidths] = useState<PanelLayout>(layout);
  const [measured, setMeasured] = useState<Measured>(UNMEASURED);
  // The elements are state, not refs: they are read when a gesture starts, and a change of
  // arrangement (the right panel is not a column below 1440 px) has to re-attach the observer.
  const [grid, setGrid] = useState<HTMLDivElement | null>(null);
  const [list, setList] = useState<HTMLElement | null>(null);
  const [details, setDetails] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => {
    if (typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    const measure = (): void => {
      const next: Measured = { container: whole(grid), list: whole(list), details: whole(details) };
      setMeasured((current) =>
        current.container === next.container && current.list === next.list && current.details === next.details
          ? current
          : next,
      );
    };
    const observer = new ResizeObserver(measure);
    for (const element of [grid, list, details]) {
      if (element !== null) {
        observer.observe(element);
      }
    }
    return () => {
      observer.disconnect();
    };
  }, [grid, list, details, wide]);

  const control = (panel: PanelId): PanelControl => {
    const element = panel === 'list' ? list : details;
    const laidOut = panel === 'list' ? measured.list : measured.details;
    return {
      panel,
      value: laidOut > 0 ? laidOut : widthOf(widths, panel),
      max: maxWidthFor(panel, widths, measured.container, wide),
      getStart: () => {
        const now = whole(element);
        return now > 0 ? now : widthOf(widths, panel);
      },
      getMax: () => maxWidthFor(panel, widths, whole(grid), wide),
      onResize: (px) => {
        setWidths(withWidth(widths, panel, px));
      },
      onCommit: (px) => {
        const next = withWidth(widths, panel, px);
        setWidths(next);
        commit(next);
      },
    };
  };

  const style = {
    '--list-w': `${String(widths.list_px)}px`,
    '--details-w': `${String(widths.details_px)}px`,
    '--list-min': `${String(limitsOf('list').min)}px`,
    '--details-min': `${String(limitsOf('details').min)}px`,
  } as CSSProperties;

  return { attachGrid: setGrid, attachList: setList, attachDetails: setDetails, style, control };
}
