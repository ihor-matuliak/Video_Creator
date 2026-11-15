import { useLayoutEffect } from 'react';
import type { RefObject } from 'react';

/** A little air between a control and the bar it was scrolled clear of, in CSS px. */
const AIR = 8;

/**
 * A sticky bar floats over the bottom of the scrolling area (`main`). A control that scrolls into
 * view "nearest", which is what a Tab focus does, stops at the very edge of that area, under the
 * bar. While the bar is `docked`, the scrolling area is told (`scroll-padding-bottom`) to keep the
 * bar's current height clear, so such a control stops just above the bar instead (docs/03 section 12).
 */
export function useClearOfStickyBar(bar: RefObject<HTMLElement | null>, docked: boolean): void {
  useLayoutEffect(() => {
    const element = bar.current;
    const scroller = element?.closest('main') ?? null;
    if (element === null || scroller === null) {
      return undefined;
    }
    const keepClear = (): void => {
      if (docked) {
        const room = Math.ceil(element.getBoundingClientRect().height) + AIR;
        scroller.style.setProperty('scroll-padding-bottom', `${String(room)}px`);
      } else {
        scroller.style.removeProperty('scroll-padding-bottom');
      }
    };
    keepClear();
    // The bar grows and shrinks (a hint, skipped clips, an error): follow it. jsdom has no layout.
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(keepClear);
    observer?.observe(element);
    return () => {
      observer?.disconnect();
      scroller.style.removeProperty('scroll-padding-bottom');
    };
  }, [bar, docked]);
}
