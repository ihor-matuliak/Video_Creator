/** The longest project name, in characters (code points): PROJECT_NAME_MAX of the backend. */
export const PROJECT_NAME_MAX_LENGTH = 120;

/** The name of a project created for a video whose file name gives nothing usable. */
export const DEFAULT_PROJECT_NAME = 'Новий проєкт';

/**
 * The project name for a picked video (docs/03 section 4, ADR 024): the file name without its
 * last extension, trimmed and cut to 120 characters. A name that starts with the only dot
 * (".mp4") has no extension, like `path.parse`. Nothing left → «Новий проєкт».
 */
export function projectNameFromFile(fileName: string): string {
  const dot = fileName.lastIndexOf('.');
  const stem = dot > 0 ? fileName.slice(0, dot) : fileName;
  const name = Array.from(stem.trim()).slice(0, PROJECT_NAME_MAX_LENGTH).join('').trim();
  return name === '' ? DEFAULT_PROJECT_NAME : name;
}

const MEBIBYTE = 1024 ** 2;
const GIBIBYTE = 1024 ** 3;

/** «12,5 ГБ» or «340 МБ»; the free space of the data folder's disk. */
export function formatFreeSpace(bytes: number): string {
  if (bytes >= GIBIBYTE) {
    return `${(bytes / GIBIBYTE).toFixed(1).replace('.', ',')} ГБ`;
  }
  return `${String(Math.floor(bytes / MEBIBYTE))} МБ`;
}
