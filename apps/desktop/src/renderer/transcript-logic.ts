/**
 * Pure helpers of the transcript part of the Моменти panel (ADR 039): the local search over the
 * segments, the cut of a segment's text at its matches (shown as <mark>, never as HTML), the
 * coverage gaps and their words, and the corrections of a segment (ADR 036). No Node, no DOM.
 */
import type {
  TranscriptCoverageSpanDto,
  TranscriptEdit,
  TranscriptSegmentDto,
  TranscriptWordDto,
} from '../shared/api';

/**
 * One match. `segment` is the index of the segment it is in, `start` and `end` are offsets in that
 * segment's text (end exclusive), `index` is its place among all the matches of the search.
 */
export interface MatchRange {
  readonly segment: number;
  readonly index: number;
  readonly start: number;
  readonly end: number;
}

/** A piece of a text: plain (`matchIndex` null) or a match with its place among the matches. */
export interface TextPart {
  readonly text: string;
  readonly matchIndex: number | null;
}

/** Characters that mean something in a regular expression; the query is searched as plain text. */
const PATTERN_SYNTAX = /[\\^$.*+?()[\]{}|]/g;

/**
 * Every match of `query` in `texts`, in reading order. The search ignores letter case and takes the
 * query as plain text (its punctuation is not a pattern). A query that is blank after trimming
 * matches nothing.
 */
export function findMatches(texts: readonly string[], query: string): readonly MatchRange[] {
  const needle = query.trim();
  if (needle === '') {
    return [];
  }
  const pattern = new RegExp(needle.replace(PATTERN_SYNTAX, '\\$&'), 'giu');
  const found: MatchRange[] = [];
  for (const [segment, text] of texts.entries()) {
    for (let hit = pattern.exec(text); hit !== null; hit = pattern.exec(text)) {
      found.push({ segment, index: found.length, start: hit.index, end: hit.index + hit[0].length });
    }
  }
  return found;
}

/** The matches grouped by the segment they are in, each group in reading order. */
export function groupBySegment(matches: readonly MatchRange[]): ReadonlyMap<number, readonly MatchRange[]> {
  const grouped = new Map<number, MatchRange[]>();
  for (const match of matches) {
    const group = grouped.get(match.segment);
    if (group === undefined) {
      grouped.set(match.segment, [match]);
    } else {
      group.push(match);
    }
  }
  return grouped;
}

/**
 * A text cut at its matches, in order: the plain pieces between them and the matches. `ranges` are
 * the matches of this text, in reading order.
 */
export function highlightParts(text: string, ranges: readonly MatchRange[]): readonly TextPart[] {
  const parts: TextPart[] = [];
  let cursor = 0;
  for (const range of ranges) {
    if (range.start > cursor) {
      parts.push({ text: text.slice(cursor, range.start), matchIndex: null });
    }
    parts.push({ text: text.slice(range.start, range.end), matchIndex: range.index });
    cursor = range.end;
  }
  if (cursor < text.length) {
    parts.push({ text: text.slice(cursor), matchIndex: null });
  }
  return parts;
}

/**
 * The match that «Попередній» (-1) or «Наступний» (+1) moves to, wrapping round both ends. With no
 * match there is nothing to move to: 0.
 */
export function stepIndex(current: number, total: number, step: 1 | -1): number {
  return total === 0 ? 0 : (current + step + total) % total;
}

/** The counter beside the search: «N із M», counting from 1, or «Збігів немає». */
export function matchCounterText(current: number, total: number): string {
  return total === 0 ? 'Збігів немає' : `${String(current + 1)} із ${String(total)}`;
}

/**
 * The spans of the coverage whose audio has no recognised text (every state but `succeeded`), in
 * time order. These are the gaps the user is shown.
 */
export function coverageGaps(
  spans: readonly TranscriptCoverageSpanDto[],
): readonly TranscriptCoverageSpanDto[] {
  return spans.filter((span) => span.state !== 'succeeded').toSorted((a, b) => a.start_ms - b.start_ms);
}

/**
 * What a gap's state says, in Ukrainian. A state the panel does not know is said as such, not
 * shown as a raw word of the backend.
 */
export function gapStateText(state: string): string {
  switch (state) {
    case 'failed':
      return 'помилка розпізнавання';
    case 'pending':
      return 'ще не розпізнано';
    case 'unknown_outcome':
      return 'невідомо, чи розпізнано';
    default:
      return 'стан невідомий';
  }
}

/** The longest text of one word in a correction (ADR 039, the backend's limit). */
export const WORD_TEXT_MAX_LENGTH = 200;
/** The longest text of a segment in a correction (ADR 039, the backend's limit). */
export const SEGMENT_TEXT_MAX_LENGTH = 2000;

/** What a correction edits: the words of a word revision, or the text of a segment (ADR 036). */
export type CorrectionUnit = 'word' | 'segment';

/** A word revision is the one whose words carry the timing; every other one is edited by segment. */
export function correctionUnitOf(timingLevel: string): CorrectionUnit {
  return timingLevel === 'word' ? 'word' : 'segment';
}

/** One text a correction can edit: the ID of the word or segment, and the text it has as stored. */
export interface CorrectionTarget {
  readonly id: string;
  readonly stored: string;
}

/** The texts a correction of one segment edits, and how many of its words the read did not bring. */
export interface CorrectionScope {
  readonly targets: readonly CorrectionTarget[];
  readonly missing: number;
}

/**
 * The texts a correction of `segment` edits: its words, in the order of its `word_ids`, for a word
 * revision; its own text otherwise. A word of `word_ids` that is not in `words` is counted in `missing`.
 */
export function correctionScope(
  segment: TranscriptSegmentDto,
  unit: CorrectionUnit,
  words: ReadonlyMap<string, TranscriptWordDto>,
): CorrectionScope {
  if (unit === 'segment') {
    return { targets: [{ id: segment.id, stored: segment.text }], missing: 0 };
  }
  const targets: CorrectionTarget[] = [];
  let missing = 0;
  for (const id of segment.word_ids) {
    const word = words.get(id);
    if (word === undefined) {
      missing += 1;
    } else {
      targets.push({ id: word.id, stored: word.text });
    }
  }
  return { targets, missing };
}

/** The drafts a correction starts with: each target's stored text. */
export function initialDrafts(targets: readonly CorrectionTarget[]): Readonly<Record<string, string>> {
  return Object.fromEntries(targets.map((target) => [target.id, target.stored]));
}

/** The text a target has in the drafts: what was typed, or the stored text while nothing was. */
export function draftText(target: CorrectionTarget, drafts: Readonly<Record<string, string>>): string {
  return drafts[target.id] ?? target.stored;
}

/** Why Save cannot be pressed: a word missing from the read, an empty field after trimming, or no change. */
export type SaveBlock = 'missing' | 'empty' | 'unchanged';

/** The reason Save is blocked, or null when the correction may be saved. */
export function saveBlockOf(scope: CorrectionScope, drafts: Readonly<Record<string, string>>): SaveBlock | null {
  if (scope.missing > 0) {
    return 'missing';
  }
  const typed = scope.targets.map((target) => ({ target, text: draftText(target, drafts).trim() }));
  if (typed.some(({ text }) => text === '')) {
    return 'empty';
  }
  return typed.some(({ target, text }) => text !== target.stored.trim()) ? null : 'unchanged';
}

/** The Ukrainian reason shown beside a blocked Save. */
export function saveBlockText(unit: CorrectionUnit, block: SaveBlock): string {
  switch (block) {
    case 'missing':
      return 'Частину слів фрагмента не вдалося завантажити, тому його не можна виправити.';
    case 'empty':
      return unit === 'word' ? 'Слово не може бути порожнім.' : 'Текст фрагмента не може бути порожнім.';
    case 'unchanged':
      return 'Немає змін: змініть текст, щоб зберегти виправлення.';
  }
}

/**
 * The edits a correction sends: each target whose trimmed text differs from its stored text, as
 * `{ word_id, text }` in a word revision and `{ segment_id, text }` otherwise. Nothing else is sent.
 */
export function correctionEdits(
  unit: CorrectionUnit,
  scope: CorrectionScope,
  drafts: Readonly<Record<string, string>>,
): TranscriptEdit[] {
  const edits: TranscriptEdit[] = [];
  for (const target of scope.targets) {
    const text = draftText(target, drafts).trim();
    if (text !== target.stored.trim()) {
      edits.push(unit === 'word' ? { word_id: target.id, text } : { segment_id: target.id, text });
    }
  }
  return edits;
}
