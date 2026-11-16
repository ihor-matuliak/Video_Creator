import { describe, expect, it } from 'vitest';

import type { TranscriptCoverageSpanDto, TranscriptSegmentDto, TranscriptWordDto } from '../shared/api';
import {
  correctionEdits,
  correctionScope,
  correctionUnitOf,
  coverageGaps,
  findMatches,
  gapStateText,
  groupBySegment,
  highlightParts,
  initialDrafts,
  matchCounterText,
  saveBlockOf,
  stepIndex,
} from './transcript-logic';

function span(startMs: number, endMs: number, state: string): TranscriptCoverageSpanDto {
  return { start_ms: startMs, end_ms: endMs, state };
}

function wordOf(id: string, text: string): TranscriptWordDto {
  return { id, ordinal: 0, start_ms: 0, end_ms: 1, text, chunk_id: null, speaker_local: null, confidence: null };
}

function segmentOf(wordIds: readonly string[], text: string): TranscriptSegmentDto {
  return { id: 'seg-1', ordinal: 0, start_ms: 0, end_ms: 1000, text, word_ids: [...wordIds], timing_level: 'word' };
}

describe('correctionUnitOf', () => {
  it('edits words only in a word revision; every other level is edited by segment', () => {
    expect(correctionUnitOf('word')).toBe('word');
    expect(correctionUnitOf('segment')).toBe('segment');
  });
});

describe('correction scope, drafts and edits (ADR 036)', () => {
  const words = new Map([
    ['w-1', wordOf('w-1', 'привіт')],
    ['w-2', wordOf('w-2', 'світ')],
  ]);

  it('takes the words of the segment in the order of its word_ids, and counts the ones the read lacks', () => {
    const scope = correctionScope(segmentOf(['w-2', 'w-9', 'w-1'], 'світ привіт'), 'word', words);
    expect(scope.targets).toEqual([
      { id: 'w-2', stored: 'світ' },
      { id: 'w-1', stored: 'привіт' },
    ]);
    expect(scope.missing).toBe(1);
  });

  it('takes the segment text as the only target in a segment revision', () => {
    const scope = correctionScope(segmentOf([], 'Текст'), 'segment', words);
    expect(scope).toEqual({ targets: [{ id: 'seg-1', stored: 'Текст' }], missing: 0 });
  });

  it('sends only the targets whose trimmed text differs, as word_id or segment_id', () => {
    const scope = correctionScope(segmentOf(['w-1', 'w-2'], 'привіт світ'), 'word', words);
    const drafts = { ...initialDrafts(scope.targets), 'w-2': '  Світе ' };
    expect(correctionEdits('word', scope, drafts)).toEqual([{ word_id: 'w-2', text: 'Світе' }]);

    const segment = correctionScope(segmentOf([], 'Текст'), 'segment', words);
    expect(correctionEdits('segment', segment, { 'seg-1': ' Текст ' })).toEqual([]);
    expect(correctionEdits('segment', segment, { 'seg-1': 'Новий' })).toEqual([
      { segment_id: 'seg-1', text: 'Новий' },
    ]);
  });

  it('gives the reason a Save is blocked: a missing word, an empty field, or no change; none when it can be saved', () => {
    const scope = correctionScope(segmentOf(['w-1', 'w-2'], 'привіт світ'), 'word', words);
    expect(saveBlockOf(scope, initialDrafts(scope.targets))).toBe('unchanged');
    expect(saveBlockOf(scope, { 'w-1': 'привіт', 'w-2': '  ' })).toBe('empty');
    expect(saveBlockOf(scope, { 'w-1': 'привіт', 'w-2': 'Світе' })).toBeNull();

    const partial = correctionScope(segmentOf(['w-1', 'w-9'], 'привіт ?'), 'word', words);
    expect(saveBlockOf(partial, { 'w-1': 'Привіт' })).toBe('missing');
  });
});

describe('findMatches', () => {
  it('finds the matches of every segment in reading order, with their places in the search', () => {
    const found = findMatches(['Привіт, привіт', 'без збігів', 'і ще Привіт'], 'привіт');
    expect(found).toEqual([
      { segment: 0, index: 0, start: 0, end: 6 },
      { segment: 0, index: 1, start: 8, end: 14 },
      { segment: 2, index: 2, start: 5, end: 11 },
    ]);
  });

  it('ignores letter case, Cyrillic included', () => {
    expect(findMatches(['ПРИВІТ Світ'], 'привіт')).toHaveLength(1);
    expect(findMatches(['світ'], 'СВІТ')).toHaveLength(1);
  });

  it('takes the query as plain text: its punctuation is not a pattern', () => {
    expect(findMatches(['axb a.b (x)'], 'a.b')).toEqual([{ segment: 0, index: 0, start: 4, end: 7 }]);
    expect(findMatches(['x (y) z'], '(y)')).toHaveLength(1);
    expect(findMatches(['a+b a*b'], 'a*b')).toEqual([{ segment: 0, index: 0, start: 4, end: 7 }]);
  });

  it('matches nothing for a blank query, and ignores the spaces around a query', () => {
    expect(findMatches(['текст'], '')).toEqual([]);
    expect(findMatches(['текст'], '   ')).toEqual([]);
    expect(findMatches(['великий текст'], '  текст ')).toEqual([{ segment: 0, index: 0, start: 8, end: 13 }]);
  });

  it('reports nothing when no segment has the query', () => {
    expect(findMatches(['один', 'два'], 'три')).toEqual([]);
  });

  it('does not carry the search position from one segment to the next', () => {
    const found = findMatches(['кіт', 'кіт'], 'кіт');
    expect(found.map((match) => match.segment)).toEqual([0, 1]);
  });
});

describe('groupBySegment', () => {
  it('groups the matches by segment, keeping their order', () => {
    const found = findMatches(['a a', '', 'a'], 'a');
    const grouped = groupBySegment(found);
    expect(grouped.get(0)?.map((match) => match.index)).toEqual([0, 1]);
    expect(grouped.get(1)).toBeUndefined();
    expect(grouped.get(2)?.map((match) => match.index)).toEqual([2]);
  });
});

describe('highlightParts', () => {
  it('cuts a text at its matches and keeps every character, in order', () => {
    const text = 'Привіт, світ. Привіт!';
    const parts = highlightParts(text, findMatches([text], 'привіт'));
    expect(parts).toEqual([
      { text: 'Привіт', matchIndex: 0 },
      { text: ', світ. ', matchIndex: null },
      { text: 'Привіт', matchIndex: 1 },
      { text: '!', matchIndex: null },
    ]);
    expect(parts.map((part) => part.text).join('')).toBe(text);
  });

  it('returns the whole text as one plain part when nothing matches', () => {
    expect(highlightParts('нічого', [])).toEqual([{ text: 'нічого', matchIndex: null }]);
  });

  it('keeps markup-like text as text: it is only ever a string here', () => {
    const text = '<b>привіт</b>';
    const parts = highlightParts(text, findMatches([text], 'привіт'));
    expect(parts.map((part) => part.text)).toEqual(['<b>', 'привіт', '</b>']);
  });

  it('returns no parts for an empty text', () => {
    expect(highlightParts('', [])).toEqual([]);
  });
});

describe('stepIndex', () => {
  it('moves to the next and the previous match', () => {
    expect(stepIndex(0, 3, 1)).toBe(1);
    expect(stepIndex(1, 3, -1)).toBe(0);
  });

  it('wraps round both ends', () => {
    expect(stepIndex(2, 3, 1)).toBe(0);
    expect(stepIndex(0, 3, -1)).toBe(2);
  });

  it('stays at 0 when there is no match', () => {
    expect(stepIndex(0, 0, 1)).toBe(0);
    expect(stepIndex(0, 0, -1)).toBe(0);
  });
});

describe('matchCounterText', () => {
  it('counts from one: «N із M»', () => {
    expect(matchCounterText(0, 3)).toBe('1 із 3');
    expect(matchCounterText(2, 3)).toBe('3 із 3');
  });

  it('says so when there is nothing found', () => {
    expect(matchCounterText(0, 0)).toBe('Збігів немає');
  });
});

describe('coverageGaps', () => {
  it('keeps the spans that are not succeeded, in time order', () => {
    const gaps = coverageGaps([
      span(9000, 12000, 'unknown_outcome'),
      span(0, 5000, 'succeeded'),
      span(5000, 9000, 'failed'),
    ]);
    expect(gaps).toEqual([span(5000, 9000, 'failed'), span(9000, 12000, 'unknown_outcome')]);
  });

  it('is empty when every span succeeded', () => {
    expect(coverageGaps([span(0, 12000, 'succeeded')])).toEqual([]);
  });

  it('keeps a state the panel does not know: it is a gap all the same', () => {
    expect(coverageGaps([span(0, 1000, 'something_new')])).toEqual([span(0, 1000, 'something_new')]);
  });
});

describe('gapStateText', () => {
  it('says the known states in Ukrainian', () => {
    expect(gapStateText('failed')).toBe('помилка розпізнавання');
    expect(gapStateText('pending')).toBe('ще не розпізнано');
    expect(gapStateText('unknown_outcome')).toBe('невідомо, чи розпізнано');
  });

  it('says an unknown state as unknown, without the raw word', () => {
    expect(gapStateText('something_new')).toBe('стан невідомий');
  });
});
