import { createContext, useContext } from 'react';

/**
 * Whether the part of the app that holds a component is on screen. The shell keeps the open
 * project mounted but hidden while «Налаштування» is open, so that the project returns as it was.
 * A project that is hidden must not react to the keyboard or go on playing: its shortcuts and its
 * player ask this. Without a provider (a screen shown on its own) the answer is yes.
 */
export const ViewActiveContext = createContext(true);

export function useViewActive(): boolean {
  return useContext(ViewActiveContext);
}
