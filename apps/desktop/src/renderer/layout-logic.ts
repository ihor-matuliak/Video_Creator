import { LAYOUT_LIMITS } from '../shared/bridge';
import type { PanelLayout } from '../shared/bridge';

/** The two resizable panels of «Моменти»; the player takes the space between them. */
export type PanelId = 'list' | 'details';

/** From this viewport width the right panel is a third column; below it, tabs under the player. */
export const WIDE_MIN_PX = 1440;
export const WIDE_QUERY = `(min-width: ${String(WIDE_MIN_PX)}px)`;
/** Width of a splitter, in CSS px (keep in step with `--splitter-width` in styles.css). */
export const SPLITTER_PX = 12;
/** The player never gets narrower than this: 520 in three columns (docs/03 section 8), 400 beside the list only. */
export const PLAYER_MIN_WIDE_PX = 520;
export const PLAYER_MIN_NARROW_PX = 400;
/** One arrow key press, and with Shift. */
export const KEY_STEP_PX = 16;
export const KEY_STEP_LARGE_PX = 64;

const FIELDS = { list: 'list_px', details: 'details_px' } as const;

export function limitsOf(panel: PanelId): { readonly min: number; readonly max: number } {
  return LAYOUT_LIMITS[FIELDS[panel]];
}

export function widthOf(layout: PanelLayout, panel: PanelId): number {
  return layout[FIELDS[panel]];
}

export function withWidth(layout: PanelLayout, panel: PanelId, px: number): PanelLayout {
  return panel === 'list' ? { ...layout, list_px: px } : { ...layout, details_px: px };
}

/**
 * `px` as a whole number inside the limits of the panel and not above `max` (the room that is
 * left once the player has its minimum). A `max` under the lower limit loses: the lower limit
 * is never broken, the grid squeezes the panel instead.
 */
export function clampWidth(panel: PanelId, px: number, max = Number.POSITIVE_INFINITY): number {
  const limits = limitsOf(panel);
  const upper = Math.max(limits.min, Math.min(limits.max, Number.isNaN(max) ? limits.max : Math.floor(max)));
  const wanted = Number.isNaN(px) ? limits.min : Math.round(px);
  return Math.min(upper, Math.max(limits.min, wanted));
}

/**
 * The widest `panel` can be inside a row `containerPx` wide: what remains after the other panel
 * (at its stored width), the splitters and the least the player may have. A container that has
 * not been measured (0) leaves only the limit of the panel itself.
 */
export function maxWidthFor(panel: PanelId, layout: PanelLayout, containerPx: number, wide: boolean): number {
  const limits = limitsOf(panel);
  if (!(containerPx > 0)) {
    return limits.max;
  }
  const other = wide ? widthOf(layout, panel === 'list' ? 'details' : 'list') : 0;
  const splitters = (wide ? 2 : 1) * SPLITTER_PX;
  const player = wide ? PLAYER_MIN_WIDE_PX : PLAYER_MIN_NARROW_PX;
  return clampWidth(panel, limits.max, containerPx - other - splitters - player);
}

/**
 * The width a key gives the panel, or null for a key that does nothing. The list grows towards
 * the right and the right panel towards the left, so each follows the splitter it owns.
 * Home and End go to the smallest and the widest.
 */
export function keyboardWidth(
  panel: PanelId,
  key: string,
  large: boolean,
  current: number,
  max: number,
): number | null {
  const step = large ? KEY_STEP_LARGE_PX : KEY_STEP_PX;
  const grow = panel === 'list' ? 'ArrowRight' : 'ArrowLeft';
  const shrink = panel === 'list' ? 'ArrowLeft' : 'ArrowRight';
  if (key === grow) {
    return clampWidth(panel, current + step, max);
  }
  if (key === shrink) {
    return clampWidth(panel, current - step, max);
  }
  if (key === 'Home') {
    return limitsOf(panel).min;
  }
  if (key === 'End') {
    return clampWidth(panel, limitsOf(panel).max, max);
  }
  return null;
}

/** The width of the panel after the pointer moved from `startX` to `x`. */
export function dragWidth(panel: PanelId, startWidth: number, startX: number, x: number, max: number): number {
  const delta = panel === 'list' ? x - startX : startX - x;
  return clampWidth(panel, startWidth + delta, max);
}
