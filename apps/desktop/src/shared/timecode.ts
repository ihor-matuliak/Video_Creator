/**
 * Source-relative time is an integer number of milliseconds (docs/04). This is
 * its one display form, HH:MM:SS.mmm. Pure: no Node, no DOM.
 */

function pad(value: number, width: number): string {
  return String(value).padStart(width, '0');
}

/** Hours are not capped: a source longer than 99 hours widens the first field. */
export function formatTimecode(ms: number): string {
  if (!Number.isSafeInteger(ms) || ms < 0) {
    throw new RangeError("Час має бути невід'ємним цілим числом мілісекунд");
  }
  const millis = ms % 1000;
  const totalSeconds = Math.floor(ms / 1000);
  const seconds = totalSeconds % 60;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const minutes = totalMinutes % 60;
  const hours = Math.floor(totalMinutes / 60);
  return `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(seconds, 2)}.${pad(millis, 3)}`;
}

// Same grammar as the backend `parse_timecode`: hours 2+ digits, minutes and seconds
// 00-59, exactly three millisecond digits, "." or ",". Only ASCII digits.
const TIMECODE = /^(\d{2,}):([0-5]\d):([0-5]\d)[.,](\d{3})$/;

/**
 * Milliseconds for text typed as HH:MM:SS.mmm, or null when it is not one.
 * Surrounding whitespace is ignored (it is typing noise, not part of the time).
 */
export function parseTimecode(text: string): number | null {
  const match = TIMECODE.exec(text.trim());
  if (match === null) {
    return null;
  }
  const [, hours, minutes, seconds, millis] = match;
  const total =
    Number(hours) * 3_600_000 + Number(minutes) * 60_000 + Number(seconds) * 1000 + Number(millis);
  return Number.isSafeInteger(total) ? total : null;
}
