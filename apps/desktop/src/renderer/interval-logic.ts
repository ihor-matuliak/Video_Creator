import { formatTimecode, parseTimecode } from '../shared/timecode';

export type IntervalCheck =
  | { readonly ok: true; readonly startMs: number; readonly endMs: number }
  | { readonly ok: false; readonly message: string };

const FORMAT_HINT = 'у форматі ГГ:ХХ:СС.мс, наприклад 00:00:04.120';

/**
 * The interval typed into the manual-clip form, checked the way the backend does
 * (docs/04): integer milliseconds, [start,end), inside [0, source duration].
 * It reads the text as it is now; nothing depends on a blur having happened.
 */
export function checkInterval(startText: string, endText: string, durationMs: number): IntervalCheck {
  const startMs = parseTimecode(startText);
  if (startMs === null) {
    return { ok: false, message: `Початок: введіть час ${FORMAT_HINT}` };
  }
  const endMs = parseTimecode(endText);
  if (endMs === null) {
    return { ok: false, message: `Кінець: введіть час ${FORMAT_HINT}` };
  }
  if (endMs <= startMs) {
    return { ok: false, message: 'Кінець має бути пізніше за початок' };
  }
  if (endMs > durationMs) {
    return {
      ok: false,
      message: `Фрагмент має вміщатися у відео: від 00:00:00.000 до ${formatTimecode(durationMs)}`,
    };
  }
  return { ok: true, startMs, endMs };
}

/**
 * The field text moved by `deltaMs` and kept inside the source; null when the
 * text is not a time yet (there is nothing to move).
 */
export function shiftTimecode(text: string, deltaMs: number, durationMs: number): string | null {
  const current = parseTimecode(text);
  if (current === null) {
    return null;
  }
  return formatTimecode(Math.min(durationMs, Math.max(0, current + deltaMs)));
}
