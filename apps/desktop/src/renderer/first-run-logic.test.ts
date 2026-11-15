import { describe, expect, it } from 'vitest';

import {
  DEFAULT_PROJECT_NAME,
  PROJECT_NAME_MAX_LENGTH,
  formatFreeSpace,
  projectNameFromFile,
} from './first-run-logic';

describe('projectNameFromFile', () => {
  it('takes the file name without its last extension', () => {
    expect(projectNameFromFile('Київ запис.mp4')).toBe('Київ запис');
    expect(projectNameFromFile('color_gop.mp4')).toBe('color_gop');
    expect(projectNameFromFile('archive.tar.mkv')).toBe('archive.tar');
  });

  it('keeps a name that has no extension', () => {
    expect(projectNameFromFile('запис')).toBe('запис');
  });

  it('does not treat a leading dot as an extension', () => {
    expect(projectNameFromFile('.mp4')).toBe('.mp4');
  });

  it('trims the name', () => {
    expect(projectNameFromFile('  Поїздка  .mov')).toBe('Поїздка');
  });

  it('cuts a long name to 120 characters and counts code points, not UTF-16 units', () => {
    const cut = projectNameFromFile(`${'а'.repeat(130)}.mp4`);
    expect(Array.from(cut)).toHaveLength(PROJECT_NAME_MAX_LENGTH);

    const emoji = projectNameFromFile(`${'🎬'.repeat(130)}.mp4`);
    expect(Array.from(emoji)).toHaveLength(PROJECT_NAME_MAX_LENGTH);
    expect(emoji).toBe('🎬'.repeat(PROJECT_NAME_MAX_LENGTH));
  });

  it('does not leave a trailing space after the cut', () => {
    const name = `${'а'.repeat(119)} ${'б'.repeat(10)}.mp4`;
    expect(projectNameFromFile(name)).toBe('а'.repeat(119));
  });

  it('falls back to «Новий проєкт» when nothing usable is left', () => {
    expect(projectNameFromFile('   .mp4')).toBe(DEFAULT_PROJECT_NAME);
    expect(projectNameFromFile('')).toBe(DEFAULT_PROJECT_NAME);
    expect(DEFAULT_PROJECT_NAME).toBe('Новий проєкт');
  });
});

describe('formatFreeSpace', () => {
  it('writes gigabytes with one decimal and a comma', () => {
    expect(formatFreeSpace(12 * 1024 ** 3)).toBe('12,0 ГБ');
    expect(formatFreeSpace(1.5 * 1024 ** 3)).toBe('1,5 ГБ');
  });

  it('writes less than a gigabyte in whole megabytes', () => {
    expect(formatFreeSpace(340 * 1024 ** 2)).toBe('340 МБ');
    expect(formatFreeSpace(0)).toBe('0 МБ');
  });
});
