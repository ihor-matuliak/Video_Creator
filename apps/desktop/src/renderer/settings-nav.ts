import { createContext, useContext } from 'react';

/**
 * Opens «Налаштування» from inside an open project. The shell owns which screen is shown, so it
 * provides this; the project stays mounted (hidden) behind the settings and comes back as it was.
 * Without a provider (a screen shown on its own) there is nothing to open: the answer is null.
 */
export const OpenSettingsContext = createContext<(() => void) | null>(null);

export function useOpenSettings(): (() => void) | null {
  return useContext(OpenSettingsContext);
}
