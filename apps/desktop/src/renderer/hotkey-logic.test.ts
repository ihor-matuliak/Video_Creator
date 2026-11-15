import { afterEach, describe, expect, it } from 'vitest';

import { activatesOnSpace, dialogIsOpen, hotkeyOf, isEditableTarget } from './hotkey-logic';
import type { KeyInfo } from './hotkey-logic';

function key(value: string, code: string, modifiers: Partial<KeyInfo> = {}): KeyInfo {
  return {
    key: value,
    code,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    shiftKey: false,
    ...modifiers,
  };
}

describe('hotkeyOf', () => {
  it('maps the keys of docs/03 section 8', () => {
    expect(hotkeyOf(key(' ', 'Space'))).toBe('play');
    expect(hotkeyOf(key('i', 'KeyI'))).toBe('start');
    expect(hotkeyOf(key('o', 'KeyO'))).toBe('end');
    expect(hotkeyOf(key('a', 'KeyA'))).toBe('approve');
    expect(hotkeyOf(key('r', 'KeyR'))).toBe('reject');
    expect(hotkeyOf(key('j', 'KeyJ'))).toBe('back');
    expect(hotkeyOf(key('l', 'KeyL'))).toBe('forward');
    expect(hotkeyOf(key('z', 'KeyZ', { ctrlKey: true }))).toBe('undo');
    expect(hotkeyOf(key('z', 'KeyZ', { metaKey: true }))).toBe('undo');
  });

  it('accepts a capital letter (Caps Lock) and a key press without a code', () => {
    expect(hotkeyOf(key('A', 'KeyA'))).toBe('approve');
    expect(hotkeyOf(key('r', ''))).toBe('reject');
    expect(hotkeyOf(key(' ', ''))).toBe('play');
  });

  it('works on the Ukrainian layout by the physical key', () => {
    expect(hotkeyOf(key('ш', 'KeyI'))).toBe('start');
    expect(hotkeyOf(key('щ', 'KeyO'))).toBe('end');
    expect(hotkeyOf(key('ф', 'KeyA'))).toBe('approve');
    expect(hotkeyOf(key('к', 'KeyR'))).toBe('reject');
    expect(hotkeyOf(key('о', 'KeyJ'))).toBe('back');
    expect(hotkeyOf(key('д', 'KeyL'))).toBe('forward');
    expect(hotkeyOf(key('я', 'KeyZ', { ctrlKey: true }))).toBe('undo');
  });

  it('follows the printed letter on a layout that moves the letters', () => {
    // Dvorak prints «c» where QWERTY has «i»: «c» is not a shortcut, «i» printed elsewhere is.
    expect(hotkeyOf(key('c', 'KeyI'))).toBeNull();
    expect(hotkeyOf(key('i', 'KeyG'))).toBe('start');
  });

  it('ignores every other key', () => {
    for (const other of ['b', 'z', 'Enter', 'Escape', 'ArrowLeft', 'Tab', '1']) {
      expect(hotkeyOf(key(other, `Key${other.toUpperCase()}`))).toBeNull();
    }
  });

  it('ignores a shortcut letter held with Ctrl, Alt, Meta or Shift, except Ctrl+Z', () => {
    for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { shiftKey: true }]) {
      expect(hotkeyOf(key('a', 'KeyA', modifiers))).toBeNull();
      expect(hotkeyOf(key('i', 'KeyI', modifiers))).toBeNull();
      expect(hotkeyOf(key(' ', 'Space', modifiers))).toBeNull();
    }
    expect(hotkeyOf(key('z', 'KeyZ', { ctrlKey: true, shiftKey: true }))).toBeNull();
    expect(hotkeyOf(key('z', 'KeyZ', { ctrlKey: true, altKey: true }))).toBeNull();
    expect(hotkeyOf(key('z', 'KeyZ'))).toBeNull();
  });
});

afterEach(() => {
  document.body.innerHTML = '';
});

function html(markup: string): Element {
  const host = document.createElement('div');
  host.innerHTML = markup;
  document.body.append(host);
  const first = host.firstElementChild;
  if (first === null) {
    throw new Error('empty markup');
  }
  return first;
}

describe('isEditableTarget', () => {
  it('is true for text fields, text areas, lists and editable content', () => {
    expect(isEditableTarget(html('<input type="text">'))).toBe(true);
    expect(isEditableTarget(html('<input>'))).toBe(true);
    expect(isEditableTarget(html('<textarea></textarea>'))).toBe(true);
    expect(isEditableTarget(html('<select><option>1</option></select>'))).toBe(true);
    expect(isEditableTarget(html('<div contenteditable="true">x</div>'))).toBe(true);
    expect(isEditableTarget(html('<div contenteditable="">x</div>'))).toBe(true);
    expect(isEditableTarget(html('<div contenteditable="true"><span id="inner">x</span></div>').querySelector('#inner'))).toBe(true);
  });

  it('is true for every input that takes text', () => {
    for (const type of ['text', 'number', 'search', 'email', 'password', 'url', 'tel', 'date', 'TEXT']) {
      expect(isEditableTarget(html(`<input type="${type}">`))).toBe(true);
    }
  });

  it('is false for an input that takes no text, so its letters stay shortcuts', () => {
    for (const type of ['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'CHECKBOX']) {
      expect(isEditableTarget(html(`<input type="${type}">`))).toBe(false);
    }
  });

  it('is false for everything else', () => {
    expect(isEditableTarget(html('<div contenteditable="false">x</div>'))).toBe(false);
    expect(isEditableTarget(html('<button>x</button>'))).toBe(false);
    expect(isEditableTarget(html('<p>x</p>'))).toBe(false);
    expect(isEditableTarget(document.body)).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget(window)).toBe(false);
  });
});

describe('activatesOnSpace', () => {
  it('is true for what Space presses', () => {
    expect(activatesOnSpace(html('<button>x</button>'))).toBe(true);
    expect(activatesOnSpace(html('<button><span id="s">x</span></button>').querySelector('#s'))).toBe(true);
    expect(activatesOnSpace(html('<a href="#x">x</a>'))).toBe(true);
    expect(activatesOnSpace(html('<div role="tab">x</div>'))).toBe(true);
    expect(activatesOnSpace(html('<summary>x</summary>'))).toBe(true);
  });

  it('is true for a checkbox and a radio button, which Space ticks', () => {
    expect(activatesOnSpace(html('<input type="checkbox">'))).toBe(true);
    expect(activatesOnSpace(html('<input type="radio">'))).toBe(true);
    expect(activatesOnSpace(html('<input type="CHECKBOX">'))).toBe(true);
  });

  it('is false for a field that takes text', () => {
    expect(activatesOnSpace(html('<input type="text">'))).toBe(false);
    expect(activatesOnSpace(html('<input>'))).toBe(false);
    expect(activatesOnSpace(html('<textarea></textarea>'))).toBe(false);
  });

  it('is false for the page itself', () => {
    expect(activatesOnSpace(document.body)).toBe(false);
    expect(activatesOnSpace(html('<p>x</p>'))).toBe(false);
    expect(activatesOnSpace(null)).toBe(false);
  });
});

describe('dialogIsOpen', () => {
  it('sees a dialog role and an open <dialog>, not a closed one', () => {
    expect(dialogIsOpen(document)).toBe(false);
    const closed = html('<dialog>x</dialog>');
    expect(dialogIsOpen(document)).toBe(false);
    closed.setAttribute('open', '');
    expect(dialogIsOpen(document)).toBe(true);
    closed.remove();
    html('<section role="dialog">x</section>');
    expect(dialogIsOpen(document)).toBe(true);
  });
});
