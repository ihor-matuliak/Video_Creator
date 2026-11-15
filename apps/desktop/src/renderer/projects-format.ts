import type { ProjectDto, ProjectListItemDto } from '../shared/api';

const PROJECT_NAME_MAX = 120;

/** FR-001: 1-120 characters after trimming; null when the name is acceptable. */
export function validateProjectName(raw: string): string | null {
  const name = raw.trim();
  if (name === '') {
    return 'Введіть назву проєкту';
  }
  // Code points, like the backend's len(): an emoji is one character, not two UTF-16 units.
  if (Array.from(name).length > PROJECT_NAME_MAX) {
    return `Назва може містити не більше ${String(PROJECT_NAME_MAX)} символів`;
  }
  return null;
}

/** Ukrainian plural of a whole number: 1, 21 -> one; 2-4, 22-24 -> few; 0, 5-20, 25-30 -> many. */
export function pluralUk(count: number, one: string, few: string, many: string): string {
  const lastTwo = count % 100;
  const last = count % 10;
  if (last === 1 && lastTwo !== 11) {
    return one;
  }
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) {
    return few;
  }
  return many;
}

/** «3 джерела» */
export function formatSourceCount(count: number): string {
  return `${String(count)} ${pluralUk(count, 'джерело', 'джерела', 'джерел')}`;
}

/** «2 кліпи» */
export function formatClipCount(count: number): string {
  return `${String(count)} ${pluralUk(count, 'кліп', 'кліпи', 'кліпів')}`;
}

/** «3 джерела · 2 кліпи» */
export function formatCounts(sources: number, clips: number): string {
  return `${formatSourceCount(sources)} · ${formatClipCount(clips)}`;
}

function twoDigits(value: number): string {
  return String(value).padStart(2, '0');
}

/** The local time as дд.мм.рррр гг:хх; «невідомо» for a timestamp that is not a date. */
export function formatActivity(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'невідомо';
  }
  const day = `${twoDigits(date.getDate())}.${twoDigits(date.getMonth() + 1)}`;
  const year = String(date.getFullYear()).padStart(4, '0');
  return `${day}.${year} ${twoDigits(date.getHours())}:${twoDigits(date.getMinutes())}`;
}

function laterOf(first: string, second: string): string {
  const firstTime = Date.parse(first);
  const secondTime = Date.parse(second);
  if (Number.isNaN(secondTime)) {
    return first;
  }
  return Number.isNaN(firstTime) || secondTime > firstTime ? second : first;
}

/**
 * The list after `updated` (the answer of an archive or an unarchive) came back: an archived
 * project leaves a list that does not show archived ones, any other keeps its figures and takes
 * the new fields. The change is itself the latest activity of the project.
 */
export function applyProjectUpdate(
  items: readonly ProjectListItemDto[],
  updated: ProjectDto,
  includeArchived: boolean,
): ProjectListItemDto[] {
  return items.flatMap((item) => {
    if (item.id !== updated.id) {
      return [item];
    }
    if (updated.archived_at !== null && !includeArchived) {
      return [];
    }
    return [{ ...item, ...updated, last_activity_at: laterOf(item.last_activity_at, updated.updated_at) }];
  });
}
