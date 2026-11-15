/** The keyboard shortcuts of the review workspace (docs/03 section 8), as pure functions. */

export type Hotkey = 'play' | 'start' | 'end' | 'approve' | 'reject' | 'back' | 'forward' | 'undo';

/** The shortcut shown on a control: `title` text and `aria-keyshortcuts` value. */
export const HOTKEY_LABELS: Readonly<Record<Hotkey, { readonly title: string; readonly aria: string }>> = {
  play: { title: 'Пробіл', aria: 'Space' },
  start: { title: 'I', aria: 'I' },
  end: { title: 'O', aria: 'O' },
  approve: { title: 'A', aria: 'A' },
  reject: { title: 'R', aria: 'R' },
  back: { title: 'J', aria: 'J' },
  forward: { title: 'L', aria: 'L' },
  undo: { title: 'Ctrl+Z', aria: 'Control+Z' },
};

const LETTER_HOTKEYS: Readonly<Record<string, Hotkey>> = {
  i: 'start',
  o: 'end',
  a: 'approve',
  r: 'reject',
  j: 'back',
  l: 'forward',
};

export interface KeyInfo {
  readonly key: string;
  readonly code: string;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
  readonly altKey: boolean;
  readonly shiftKey: boolean;
}

/**
 * The letter of a key press. On a Latin layout it is the printed letter; on another script
 * (the Ukrainian layout prints «ш» where «I» is) the physical key decides, so the shortcuts
 * work without switching the layout.
 */
function letterOf(event: KeyInfo): string | null {
  const key = event.key.toLowerCase();
  if (/^[a-z]$/.test(key)) {
    return key;
  }
  const physical = /^Key([A-Z])$/.exec(event.code);
  return physical?.[1]?.toLowerCase() ?? null;
}

/** The shortcut a key press stands for; null for every other key and for any other modifier. */
export function hotkeyOf(event: KeyInfo): Hotkey | null {
  if (event.shiftKey || event.altKey) {
    return null;
  }
  const letter = letterOf(event);
  if (event.ctrlKey || event.metaKey) {
    return letter === 'z' ? 'undo' : null;
  }
  if (event.code === 'Space' || event.key === ' ') {
    return 'play';
  }
  return letter === null ? null : (LETTER_HOTKEYS[letter] ?? null);
}

/** The input types that take no text: they are pressed, ticked or picked, so the letters stay shortcuts. */
const NON_TEXT_INPUTS = ['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'];

const TEXT_INPUT = NON_TEXT_INPUTS.map((type) => `:not([type="${type}" i])`).join('');

const EDITABLE_SELECTOR = `input${TEXT_INPUT}, textarea, select, [contenteditable]:not([contenteditable="false"])`;

/** Text is being typed here (or chosen from a list): the key belongs to the control, not to a shortcut. */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) {
    return false;
  }
  return target.closest(EDITABLE_SELECTOR) !== null;
}

/** A control that Space activates by itself: Space must not also toggle the playback. */
export function activatesOnSpace(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest(
      'button, a[href], summary, input[type="checkbox" i], input[type="radio" i], input[type="button" i], input[type="submit" i], input[type="reset" i], input[type="file" i], input[type="color" i], [role="button"], [role="tab"], [role="checkbox"]',
    ) !== null
  );
}

/** Any dialog is open: the keys are the dialog's. */
export function dialogIsOpen(doc: Document): boolean {
  return doc.querySelector('[role="dialog"], [role="alertdialog"], dialog[open]') !== null;
}
