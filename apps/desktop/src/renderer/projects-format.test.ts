import { describe, expect, it } from 'vitest';

import {
  applyProjectUpdate,
  formatActivity,
  formatClipCount,
  formatCounts,
  formatSourceCount,
  pluralUk,
} from './projects-format';
import { makeProject, makeProjectItem } from './test-bridge';

describe('pluralUk', () => {
  it.each([
    [0, 'many'],
    [1, 'one'],
    [2, 'few'],
    [3, 'few'],
    [4, 'few'],
    [5, 'many'],
    [9, 'many'],
    [10, 'many'],
    [11, 'many'],
    [12, 'many'],
    [13, 'many'],
    [14, 'many'],
    [15, 'many'],
    [19, 'many'],
    [20, 'many'],
    [21, 'one'],
    [22, 'few'],
    [24, 'few'],
    [25, 'many'],
    [100, 'many'],
    [101, 'one'],
    [111, 'many'],
    [112, 'many'],
    [121, 'one'],
    [1002, 'few'],
  ])('%i -> %s', (count, form) => {
    expect(pluralUk(count, 'one', 'few', 'many')).toBe(form);
  });
});

describe('counts of a project', () => {
  it('words the sources: 1 джерело, 2 джерела, 5 джерел', () => {
    expect(formatSourceCount(0)).toBe('0 джерел');
    expect(formatSourceCount(1)).toBe('1 джерело');
    expect(formatSourceCount(2)).toBe('2 джерела');
    expect(formatSourceCount(4)).toBe('4 джерела');
    expect(formatSourceCount(5)).toBe('5 джерел');
    expect(formatSourceCount(11)).toBe('11 джерел');
    expect(formatSourceCount(21)).toBe('21 джерело');
    expect(formatSourceCount(23)).toBe('23 джерела');
  });

  it('words the clips: 1 кліп, 2 кліпи, 5 кліпів', () => {
    expect(formatClipCount(0)).toBe('0 кліпів');
    expect(formatClipCount(1)).toBe('1 кліп');
    expect(formatClipCount(3)).toBe('3 кліпи');
    expect(formatClipCount(12)).toBe('12 кліпів');
    expect(formatClipCount(21)).toBe('21 кліп');
    expect(formatClipCount(104)).toBe('104 кліпи');
  });

  it('joins them with a middle dot', () => {
    expect(formatCounts(1, 2)).toBe('1 джерело · 2 кліпи');
    expect(formatCounts(0, 0)).toBe('0 джерел · 0 кліпів');
  });
});

describe('formatActivity', () => {
  it('shows the local time as дд.мм.рррр гг:хх with leading zeros', () => {
    // Built from local fields, so the expectation holds in any time zone.
    expect(formatActivity(new Date(2026, 10, 4, 9, 5).toISOString())).toBe('04.11.2026 09:05');
    expect(formatActivity(new Date(2026, 0, 31, 23, 59, 59).toISOString())).toBe('31.01.2026 23:59');
    expect(formatActivity(new Date(2026, 11, 1, 0, 0).toISOString())).toBe('01.12.2026 00:00');
  });

  it('reads the fixed-width UTC stamps of the backend', () => {
    const stamp = '2026-11-14T10:15:30.123456Z';
    const local = new Date(stamp);
    const two = (value: number): string => String(value).padStart(2, '0');

    expect(formatActivity(stamp)).toBe(
      `${two(local.getDate())}.${two(local.getMonth() + 1)}.${String(local.getFullYear())} ` +
        `${two(local.getHours())}:${two(local.getMinutes())}`,
    );
  });

  it('says «невідомо» for something that is not a date', () => {
    expect(formatActivity('')).toBe('невідомо');
    expect(formatActivity('учора')).toBe('невідомо');
  });
});

describe('applyProjectUpdate', () => {
  const first = makeProjectItem({ id: '11111111-2222-4333-8444-555555555551', source_count: 2, clip_count: 3 });
  const second = makeProjectItem({ id: '11111111-2222-4333-8444-555555555552', name: 'Другий' });
  const archivedAt = '2026-11-15T08:00:00Z';

  it('drops an archived project from a list without archived ones', () => {
    const updated = makeProject({ id: first.id, updated_at: archivedAt, archived_at: archivedAt });

    expect(applyProjectUpdate([first, second], updated, false)).toEqual([second]);
  });

  it('keeps it, marked, in a list with archived ones, with its figures and the change as the latest activity', () => {
    const updated = makeProject({ id: first.id, updated_at: archivedAt, archived_at: archivedAt });

    const [changed, other] = applyProjectUpdate([first, second], updated, true);

    expect(changed).toEqual({
      ...first,
      updated_at: archivedAt,
      archived_at: archivedAt,
      last_activity_at: archivedAt,
    });
    expect(changed?.source_count).toBe(2);
    expect(changed?.clip_count).toBe(3);
    expect(other).toBe(second);
  });

  it('does not move the last activity back when a clip is newer than the project change', () => {
    const busy = makeProjectItem({ id: first.id, last_activity_at: '2026-11-16T09:00:00Z' });
    const updated = makeProject({ id: first.id, updated_at: archivedAt, archived_at: null });

    expect(applyProjectUpdate([busy], updated, true)[0]?.last_activity_at).toBe('2026-11-16T09:00:00Z');
  });

  it('leaves the list as it is for a project that is not in it', () => {
    const updated = makeProject({ id: '11111111-2222-4333-8444-555555555559', archived_at: archivedAt });

    expect(applyProjectUpdate([first, second], updated, false)).toEqual([first, second]);
  });
});
