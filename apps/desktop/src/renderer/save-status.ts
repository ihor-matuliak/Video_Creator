import type { Tone } from './ui';
import type { SaveState } from './use-clip-editing';

/** What the project header says about the autosave of the clips being edited (docs/03 section 2). */
export type SaveStatus = 'saved' | 'saving' | 'error' | 'conflict';

/**
 * One status for all the clips of a workspace. The worst one wins, because the user must not be
 * told «Збережено» while any clip is in conflict, was refused or is still being stored.
 */
export function summarizeSaveStates(states: Iterable<SaveState>): SaveStatus {
  let saving = false;
  let failed = false;
  for (const state of states) {
    if (state.kind === 'conflict') {
      return 'conflict';
    }
    failed = failed || state.kind === 'error';
    saving = saving || state.kind === 'saving';
  }
  if (failed) {
    return 'error';
  }
  return saving ? 'saving' : 'saved';
}

/** The header shows each status as an icon (from the tone) and a text, never colour alone. */
export const SAVE_STATUS_VIEW: Readonly<Record<SaveStatus, { readonly tone: Tone; readonly label: string }>> = {
  saving: { tone: 'neutral', label: 'Зберігаємо…' },
  saved: { tone: 'success', label: 'Збережено' },
  error: { tone: 'danger', label: 'Не вдалося зберегти' },
  conflict: { tone: 'warning', label: 'Конфлікт змін' },
};
