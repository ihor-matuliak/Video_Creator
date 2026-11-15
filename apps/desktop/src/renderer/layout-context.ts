import { createContext, useContext } from 'react';

import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { PanelLayout } from '../shared/bridge';

/** The panel widths on screen and the way to remember a change (the shell saves them in main). */
export interface LayoutControl {
  readonly layout: PanelLayout;
  /** The user let go of a splitter or pressed a key on it: remember `next`. */
  readonly commit: (next: PanelLayout) => void;
}

const FALLBACK: LayoutControl = { layout: DEFAULT_LAYOUT, commit: () => undefined };

/** Without a provider (a screen shown on its own) the widths are the defaults and nothing is saved. */
export const LayoutContext = createContext<LayoutControl>(FALLBACK);

export function useLayoutControl(): LayoutControl {
  return useContext(LayoutContext);
}
