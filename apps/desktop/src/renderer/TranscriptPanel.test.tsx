import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ApiParams,
  TranscriptCoverageSpanDto,
  TranscriptDetailDto,
  TranscriptDto,
  TranscriptEdit,
  TranscriptSegmentDto,
  TranscriptWordDto,
} from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { TranscriptPanel } from './TranscriptPanel';
import { SOURCE_ID, failure, flushAnswers, installBridge, ok } from './test-bridge';
import type { InstalledBridge } from './test-bridge';

// ADR 039: the «Транскрипція» part of the Моменти panel. The backend is a fake that answers the
// revisions and their windows; the panel is rendered on its own.

const REV_OLD = '3c1e7a2b-4d5f-4e6a-8b9c-0d1e2f3a4b5c';
const REV_NEW = '7f2d9b4e-1a3c-4b5d-9e8f-6a7b8c9d0e1f';
const OTHER_SOURCE_ID = '2b4d6f8a-1c3e-4a5b-8d7e-9f0a1b2c3d4e';
const CREATED_AT = '2026-11-16T10:00:00Z';

/** The elements that were scrolled into view, in order: jsdom has no layout to scroll. */
let scrolled: Element[] = [];

beforeEach(() => {
  scrolled = [];
  Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
    scrolled.push(this);
  };
});

afterEach(() => {
  vi.restoreAllMocks();
});

function spanOf(startMs: number, endMs: number, state: string): TranscriptCoverageSpanDto {
  return { start_ms: startMs, end_ms: endMs, state };
}

function revisionOf(id: string, revision: number, overrides: Partial<TranscriptDto> = {}): TranscriptDto {
  return {
    id,
    source_id: SOURCE_ID,
    revision,
    origin: 'stt',
    run_id: null,
    parent_id: null,
    timing_level: 'segment',
    language: 'uk',
    duration_ms: 12_000,
    word_count: 0,
    segment_count: 0,
    coverage: [spanOf(0, 12_000, 'succeeded')],
    created_at: CREATED_AT,
    ...overrides,
  };
}

function segmentOf(ordinal: number, text: string, startMs: number): TranscriptSegmentDto {
  return {
    id: `seg-${String(ordinal)}`,
    ordinal,
    start_ms: startMs,
    end_ms: startMs + 1000,
    text,
    word_ids: [],
    timing_level: 'segment',
  };
}

/** One window of a revision, as `transcripts.get` answers it. */
function windowOf(
  revision: TranscriptDto,
  segments: readonly TranscriptSegmentDto[],
  offset: number,
  nextOffset: number | null,
): TranscriptDetailDto {
  return { ...revision, offset, limit: 1000, next_offset: nextOffset, segments: [...segments], words: [] };
}

/** The params of every call of one op, in order. */
function paramsOf(installed: InstalledBridge, op: string): unknown[] {
  return installed.call.mock.calls.filter(([called]) => called === op).map(([, params]) => params);
}

/**
 * The focus is off the fields while a save is on its way: the browser moves it to the clicked button,
 * which the save disables, and drops it there. jsdom keeps the focus on a disabled element, so the test
 * moves it off before the click instead.
 */
function dropFocus(): void {
  if (document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
}

/**
 * A backend with one revision of the source, whose segments are the given texts, one every 4.12 s
 * (the second segment starts at 4120 ms). The coverage is the one given, or a succeeded span.
 */
function installRevision(
  texts: readonly string[],
  coverage: readonly TranscriptCoverageSpanDto[] = [spanOf(0, 12_000, 'succeeded')],
): InstalledBridge {
  const revision = revisionOf(REV_NEW, 1, { coverage: [...coverage] });
  const segments = texts.map((text, index) => segmentOf(index, text, index * 4120));
  return installBridge({
    handlers: {
      'transcripts.list': () => ok({ items: [revision], next_cursor: null }),
      'transcripts.get': () => ok(windowOf(revision, segments, 0, null)),
    },
  });
}

function renderPanel(sourceId: string = SOURCE_ID) {
  const onSeek = vi.fn<(ms: number) => void>();
  const view = render(<TranscriptPanel sourceId={sourceId} onSeek={onSeek} />);
  return { onSeek, ...view };
}

describe('Транскрипція: reading the newest revision', () => {
  it('follows the cursor of the revisions to the last page and reads the newest one', async () => {
    const older = revisionOf(REV_OLD, 1);
    const newer = revisionOf(REV_NEW, 2);
    const installed = installBridge({
      handlers: {
        'transcripts.list': (params) =>
          params.cursor === undefined
            ? ok({ items: [older], next_cursor: 'page-2' })
            : ok({ items: [newer], next_cursor: null }),
        'transcripts.get': () => ok(windowOf(newer, [segmentOf(0, 'новіший текст', 0)], 0, null)),
      },
    });
    renderPanel();

    expect(await screen.findByText('новіший текст')).toBeInTheDocument();
    expect(screen.getByTestId('transcript-revision')).toHaveTextContent('Ревізія 2');
    expect(paramsOf(installed, 'transcripts.list')).toEqual([
      { source_id: SOURCE_ID },
      { source_id: SOURCE_ID, cursor: 'page-2' },
    ]);
    expect(paramsOf(installed, 'transcripts.get')).toEqual([{ id: REV_NEW, offset: 0, limit: 1000 }]);
  });

  it('reads every window of the revision, following next_offset', async () => {
    const revision = revisionOf(REV_NEW, 1);
    const installed = installBridge({
      handlers: {
        'transcripts.list': () => ok({ items: [revision], next_cursor: null }),
        'transcripts.get': (params) =>
          params.offset === 0
            ? ok(windowOf(revision, [segmentOf(0, 'перший', 0), segmentOf(1, 'другий', 1000)], 0, 2))
            : ok(windowOf(revision, [segmentOf(2, 'третій', 2000)], 2, null)),
      },
    });
    renderPanel();

    expect(await screen.findByText('третій')).toBeInTheDocument();
    expect(screen.getAllByTestId('transcript-segment')).toHaveLength(3);
    expect(paramsOf(installed, 'transcripts.get')).toEqual([
      { id: REV_NEW, offset: 0, limit: 1000 },
      { id: REV_NEW, offset: 2, limit: 1000 },
    ]);
  });

  it('says there is no transcript yet and points at «Джерела», without reading any window', async () => {
    const installed = installBridge({
      handlers: { 'transcripts.list': () => ok({ items: [], next_cursor: null }) },
    });
    renderPanel();

    expect(await screen.findByTestId('transcript-none')).toHaveTextContent('Відкрийте вкладку «Джерела»');
    expect(screen.queryByTestId('transcript-segments')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.get')).toEqual([]);
  });

  it('shows the error of a failed read, and a retry that can succeed', async () => {
    let attempts = 0;
    const revision = revisionOf(REV_NEW, 1);
    installBridge({
      handlers: {
        'transcripts.list': () => {
          attempts += 1;
          return attempts === 1
            ? failure('INTERNAL_ERROR', 'Внутрішня помилка', true)
            : ok({ items: [revision], next_cursor: null });
        },
        'transcripts.get': () => ok(windowOf(revision, [segmentOf(0, 'текст після повтору', 0)], 0, null)),
      },
    });
    renderPanel();

    const alert = await screen.findByTestId('transcript-error');
    expect(alert).toHaveTextContent('Внутрішня помилка');
    fireEvent.click(within(alert).getByRole('button', { name: 'Спробувати ще раз' }));
    expect(await screen.findByText('текст після повтору')).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('stops after too many pages instead of reading on, and says so', async () => {
    const installed = installBridge({
      handlers: { 'transcripts.list': () => ok({ items: [], next_cursor: 'again' }) },
    });
    renderPanel();

    expect(await screen.findByTestId('transcript-error')).toHaveTextContent(
      'Сервер повертає забагато частин транскрипту',
    );
    expect(paramsOf(installed, 'transcripts.list')).toHaveLength(500);
  });
});

/** Rows of ROW_PX, a list that shows LIST_PX, and marks MATCH_PX tall: jsdom has no layout to measure. */
const ROW_PX = 40;
const LIST_PX = 100;
const MATCH_PX = 20;

function boxAt(top: number, height: number): DOMRect {
  return { top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}) };
}

/**
 * Lays the list out the way a browser does for these numbers: its scrollTop stays within its content,
 * and every row and mark is where that scrollTop puts it. Returns the list.
 */
function layOutList(): HTMLElement {
  const list = screen.getByTestId('transcript-segments');
  let top = 0;
  const maxTop = Math.max(list.children.length * ROW_PX - LIST_PX, 0);
  Object.defineProperty(list, 'clientHeight', { configurable: true, value: LIST_PX });
  Object.defineProperty(list, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (value: number) => {
      top = Math.min(Math.max(value, 0), maxTop);
    },
  });
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this === list) {
      return boxAt(0, LIST_PX);
    }
    const row = this.closest('[data-testid="transcript-segment"]');
    if (row?.parentElement !== list) {
      return boxAt(0, 0);
    }
    const index = Array.from(list.children).indexOf(row);
    return boxAt(index * ROW_PX - top, this.tagName === 'MARK' ? MATCH_PX : ROW_PX);
  });
  return list;
}

/** The match is inside the part of the list that shows: it is the list that was scrolled to it. */
function expectInView(list: HTMLElement, index: number): void {
  const row = list.querySelector(`[data-match="${String(index)}"]`)?.closest('[data-testid="transcript-segment"]');
  const rowIndex = row === null || row === undefined ? -1 : Array.from(list.children).indexOf(row);
  expect(rowIndex).toBeGreaterThanOrEqual(0);
  expect(rowIndex * ROW_PX).toBeGreaterThanOrEqual(list.scrollTop);
  expect(rowIndex * ROW_PX + MATCH_PX).toBeLessThanOrEqual(list.scrollTop + LIST_PX);
}

describe('Транскрипція: search', () => {
  it('counts the matches, steps through them with wrap-around and brings each one into view', async () => {
    installRevision(['Привіт, привіт', 'Тут без слова', 'І знову ПРИВІТ']);
    const { container } = renderPanel();
    const search = await screen.findByRole('searchbox', { name: 'Пошук у тексті' });
    const list = layOutList();

    fireEvent.change(search, { target: { value: 'привіт' } });
    const count = screen.getByTestId('transcript-count');
    expect(count).toHaveTextContent('1 із 3');
    expect(Array.from(container.querySelectorAll('mark'), (mark) => mark.textContent)).toEqual([
      'Привіт',
      'привіт',
      'ПРИВІТ',
    ]);
    expectInView(list, 0);

    fireEvent.click(screen.getByRole('button', { name: 'Наступний' }));
    expect(count).toHaveTextContent('2 із 3');
    expectInView(list, 1);
    expect(container.querySelector('mark[aria-current="true"]')?.textContent).toBe('привіт');

    fireEvent.click(screen.getByRole('button', { name: 'Попередній' }));
    fireEvent.click(screen.getByRole('button', { name: 'Попередній' }));
    expect(count).toHaveTextContent('3 із 3');
    expectInView(list, 2);

    // «Наступний» from the last match goes back to the first one.
    fireEvent.click(screen.getByRole('button', { name: 'Наступний' }));
    expect(count).toHaveTextContent('1 із 3');
    expectInView(list, 0);
    expect(scrolled).toEqual([]);
  });

  it('centres a match by moving the list alone: the page is never scrolled', async () => {
    installRevision(Array.from({ length: 10 }, (_, index) => (index === 5 ? 'ціль' : `рядок ${String(index)}`)));
    renderPanel();
    await screen.findByTestId('transcript-segments');
    const list = layOutList();
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);

    fireEvent.change(screen.getByRole('searchbox', { name: 'Пошук у тексті' }), { target: { value: 'ціль' } });
    // Row 5 starts 200 px down; the list shows 100 px, so its top is 200 - (100 - 20) / 2 = 160 px.
    expect(list.scrollTop).toBe(160);
    expectInView(list, 0);
    expect(scrolled).toEqual([]);
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it('stops at the end of the list when a match is near the bottom, and does not scroll past it', async () => {
    installRevision(Array.from({ length: 10 }, (_, index) => (index === 9 ? 'кінець' : `рядок ${String(index)}`)));
    renderPanel();
    await screen.findByTestId('transcript-segments');
    const list = layOutList();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Пошук у тексті' }), { target: { value: 'кінець' } });
    // The content is 400 px, the list shows 100 px: the last 100 px are as far as it goes.
    expect(list.scrollTop).toBe(300);
    expectInView(list, 0);
    expect(scrolled).toEqual([]);
  });

  it('says when nothing matches, leaves the steps with nothing to move to, and shows no counter for a blank query', async () => {
    installRevision(['один', 'два']);
    renderPanel();
    const search = await screen.findByRole('searchbox', { name: 'Пошук у тексті' });

    fireEvent.change(search, { target: { value: 'три' } });
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('Збігів немає');
    expect(screen.getByRole('button', { name: 'Наступний' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Попередній' })).toBeDisabled();

    fireEvent.change(search, { target: { value: '   ' } });
    expect(screen.queryByTestId('transcript-count')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Наступний' })).toBeDisabled();
  });

  it('starts again at the first match when the query changes', async () => {
    installRevision(['кіт і кіт', 'собака']);
    renderPanel();
    const search = await screen.findByRole('searchbox', { name: 'Пошук у тексті' });

    fireEvent.change(search, { target: { value: 'кіт' } });
    fireEvent.click(screen.getByRole('button', { name: 'Наступний' }));
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('2 із 2');

    fireEvent.change(search, { target: { value: 'кі' } });
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('1 із 2');
  });

  it('shows text with markup as text: <b> and <img> are not tags here', async () => {
    const text = '<b>привіт</b> <img src="x">';
    installRevision([text]);
    const { container } = renderPanel();

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(container.querySelector('b, img')).toBeNull();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Пошук у тексті' }), {
      target: { value: 'привіт' },
    });
    expect(container.querySelector('b, img')).toBeNull();
    expect(container.querySelector('mark')?.textContent).toBe('привіт');
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent(text);
  });

  it('scrolls to the new match when a new query has the same count and the same index', async () => {
    installRevision(['кіт', ...Array.from({ length: 8 }, (_, index) => `рядок ${String(index + 1)}`), 'ціль']);
    renderPanel();
    const search = await screen.findByRole('searchbox', { name: 'Пошук у тексті' });
    const list = layOutList();

    fireEvent.change(search, { target: { value: 'кіт' } });
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('1 із 1');
    expectInView(list, 0);

    // Both queries have one match, at index 0: the match itself is what differs, so the list must follow it.
    fireEvent.change(search, { target: { value: 'ціль' } });
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('1 із 1');
    expect(list.querySelector('[data-match="0"]')).toHaveTextContent('ціль');
    expectInView(list, 0);
  });
});

describe('Транскрипція: seeking', () => {
  it('seeks the player to the start of a segment when its time is clicked', async () => {
    installRevision(['перший', 'другий', 'третій']);
    const { onSeek } = renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: '00:00:04.120' }));
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(4120);
  });

  it('lists only the spans that are not succeeded, in time order, and seeks to the start of one', async () => {
    installRevision(['перший', 'другий'], [
      spanOf(9_000, 12_000, 'unknown_outcome'),
      spanOf(0, 5_000, 'succeeded'),
      spanOf(5_000, 9_000, 'failed'),
    ]);
    const { onSeek } = renderPanel();

    const gaps = await screen.findAllByTestId('transcript-gap');
    expect(gaps).toHaveLength(2);
    expect(gaps[0]).toHaveTextContent('00:00:05.000–00:00:09.000 · помилка розпізнавання');
    expect(gaps[1]).toHaveTextContent('00:00:09.000–00:00:12.000 · невідомо, чи розпізнано');

    fireEvent.click(screen.getByRole('button', { name: '00:00:05.000–00:00:09.000 · помилка розпізнавання' }));
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(5000);
  });

  it('shows no gap list when every span succeeded', async () => {
    installRevision(['перший'], [spanOf(0, 12_000, 'succeeded')]);
    renderPanel();

    await screen.findByTestId('transcript-segments');
    expect(screen.queryByTestId('transcript-gap')).not.toBeInTheDocument();
  });
});

describe('Транскрипція: the source', () => {
  it('drops the answer of a source that is no longer shown', async () => {
    const revisionA = revisionOf(REV_OLD, 1);
    const revisionB = revisionOf(REV_NEW, 1);
    let releaseA = (): void => undefined;
    const gate = new Promise<void>((resolve) => {
      releaseA = () => {
        resolve();
      };
    });
    const installed = installBridge({
      handlers: {
        'transcripts.list': async (params) => {
          if (params.source_id === SOURCE_ID) {
            await gate;
            return ok({ items: [revisionA], next_cursor: null });
          }
          return ok({ items: [revisionB], next_cursor: null });
        },
        'transcripts.get': (params) =>
          params.id === REV_OLD
            ? ok(windowOf(revisionA, [segmentOf(0, 'текст першого джерела', 0)], 0, null))
            : ok(windowOf(revisionB, [segmentOf(0, 'текст другого джерела', 0)], 0, null)),
      },
    });
    const { rerender, onSeek } = renderPanel(SOURCE_ID);
    expect(screen.getByTestId('transcript-loading')).toBeInTheDocument();

    rerender(<TranscriptPanel sourceId={OTHER_SOURCE_ID} onSeek={onSeek} />);
    expect(await screen.findByText('текст другого джерела')).toBeInTheDocument();

    // The first source answers late: its revision is not read, and nothing of it is shown.
    await act(async () => {
      releaseA();
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(screen.queryByText('текст першого джерела')).not.toBeInTheDocument();
    expect(screen.getByText('текст другого джерела')).toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.get')).toEqual([{ id: REV_NEW, offset: 0, limit: 1000 }]);
  });
});

// ADR 036 and 039: «Виправити» of a segment. The fake backend saves a correction as a new revision,
// the way the real one does: the edited words and segments keep their IDs and times, and a segment
// of a word revision takes its text from its words, joined by one space.

const CORRECTED_ID = 'b0d1c2e3-4f5a-4b6c-8d7e-9f0a1b2c3d4e';

/** What the fake backend keeps: the newest revision with its segments and words. */
interface Stored {
  readonly revision: TranscriptDto;
  readonly segments: readonly TranscriptSegmentDto[];
  readonly words: readonly TranscriptWordDto[];
}

function wordOf(id: string, ordinal: number, text: string): TranscriptWordDto {
  return {
    id,
    ordinal,
    start_ms: ordinal * 500,
    end_ms: ordinal * 500 + 400,
    text,
    chunk_id: null,
    speaker_local: null,
    confidence: null,
  };
}

function wordSegmentOf(ordinal: number, text: string, wordIds: readonly string[]): TranscriptSegmentDto {
  return {
    id: `seg-${String(ordinal)}`,
    ordinal,
    start_ms: ordinal * 4120,
    end_ms: ordinal * 4120 + 1000,
    text,
    word_ids: [...wordIds],
    timing_level: 'word',
  };
}

/** A segment-level revision with one segment, «Перший текст» or the text given. */
function segmentStored(text = 'Перший текст'): Stored {
  return {
    revision: revisionOf(REV_NEW, 1, { segment_count: 1 }),
    segments: [segmentOf(0, text, 0)],
    words: [],
  };
}

/** A segment-level revision whose segments are the texts given, one every 4.12 s. */
function segmentsStored(texts: readonly string[]): Stored {
  return {
    revision: revisionOf(REV_NEW, 1, { segment_count: texts.length }),
    segments: texts.map((text, index) => segmentOf(index, text, index * 4120)),
    words: [],
  };
}

/** A word revision with one segment «привіт світ !» whose words are w-1, w-2 and w-3. */
function wordStored(): Stored {
  return {
    revision: revisionOf(REV_NEW, 1, { timing_level: 'word', segment_count: 1, word_count: 3 }),
    segments: [wordSegmentOf(0, 'привіт світ !', ['w-1', 'w-2', 'w-3'])],
    words: [wordOf('w-1', 0, 'привіт'), wordOf('w-2', 1, 'світ'), wordOf('w-3', 2, '!')],
  };
}

/** The window of a revision that `transcripts.get` answers: at most `size` items of each list. */
function windowOfStored(stored: Stored, offset: number, limit: number, size: number): TranscriptDetailDto {
  const end = offset + Math.min(limit, size);
  const hasMore = end < Math.max(stored.segments.length, stored.words.length);
  return {
    ...stored.revision,
    offset,
    limit,
    next_offset: hasMore ? end : null,
    segments: stored.segments.slice(offset, end),
    words: stored.words.slice(offset, end),
  };
}

/** The revision a correction makes, as the backend builds it (ADR 036). */
function correctedOf(stored: Stored, edits: readonly TranscriptEdit[]): Stored {
  const wordEdits = new Map<string, string>();
  const segmentEdits = new Map<string, string>();
  for (const edit of edits) {
    if ('word_id' in edit) {
      wordEdits.set(edit.word_id, edit.text);
    } else {
      segmentEdits.set(edit.segment_id, edit.text);
    }
  }
  const words = stored.words.map((word) => ({ ...word, text: wordEdits.get(word.id) ?? word.text }));
  const textOf = new Map(words.map((word) => [word.id, word.text]));
  const segments = stored.segments.map((segment) => ({
    ...segment,
    text: segment.word_ids.some((id) => wordEdits.has(id))
      ? segment.word_ids.map((id) => textOf.get(id) ?? '').join(' ')
      : (segmentEdits.get(segment.id) ?? segment.text),
  }));
  return {
    revision: {
      ...stored.revision,
      id: CORRECTED_ID,
      revision: stored.revision.revision + 1,
      origin: 'correction',
      parent_id: stored.revision.id,
    },
    segments,
    words,
  };
}

interface CorrectableOptions {
  /** Items of each list in one window; the backend's windows are at most 1000 items. */
  readonly size?: number;
  /** Answers `transcripts.correct` instead of saving the correction. */
  readonly correct?: (params: ApiParams['transcripts.correct']) => ApiResult<TranscriptDto>;
  /** The reads that come after the first save wait for this promise. */
  readonly holdReads?: Promise<void>;
}

/** A backend whose newest revision can be corrected; a saved correction is the newest from then on. */
function installCorrectable(initial: Stored, options: CorrectableOptions = {}): InstalledBridge {
  let stored = initial;
  let saved = false;
  return installBridge({
    handlers: {
      'transcripts.list': async () => {
        if (saved && options.holdReads !== undefined) {
          await options.holdReads;
        }
        return ok({ items: [stored.revision], next_cursor: null });
      },
      'transcripts.get': (params) =>
        ok(windowOfStored(stored, params.offset ?? 0, params.limit ?? 1000, options.size ?? 1000)),
      'transcripts.correct': (params) => {
        if (options.correct !== undefined) {
          return options.correct(params);
        }
        stored = correctedOf(stored, params.edits);
        saved = true;
        return ok(stored.revision);
      },
    },
  });
}

function twoSegmentsStored(): Stored {
  return {
    revision: revisionOf(REV_NEW, 1, { segment_count: 2 }),
    segments: [segmentOf(0, 'Перший', 0), segmentOf(1, 'Другий', 4120)],
    words: [],
  };
}

function nth<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) {
    throw new Error(`немає елемента ${String(index)}`);
  }
  return item;
}

describe('Виправлення: «Виправити» of a segment (ADR 036, 039)', () => {
  it('a word revision shows one field per word of the segment, at most 200 characters each', async () => {
    installCorrectable(wordStored());
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const editor = screen.getByTestId('transcript-correction');
    expect(within(editor).getAllByRole('textbox')).toHaveLength(3);
    expect(within(editor).getByRole('textbox', { name: 'Слово 1' })).toHaveValue('привіт');
    expect(within(editor).getByRole('textbox', { name: 'Слово 2' })).toHaveValue('світ');
    expect(within(editor).getByRole('textbox', { name: 'Слово 3' })).toHaveAttribute('maxlength', '200');
  });

  it('a word edit sends only the changed word_id, then shows the new revision with focus back on «Виправити»', async () => {
    const installed = installCorrectable(wordStored());
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Слово 2' }), { target: { value: 'Світе' } });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    expect(await screen.findByTestId('correction-saved')).toHaveTextContent('Виправлення збережено: ревізія 2');
    expect(screen.getByTestId('transcript-revision')).toHaveTextContent('Ревізія 2');
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent('привіт Світе !');
    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.correct')).toEqual([
      { id: REV_NEW, edits: [{ word_id: 'w-2', text: 'Світе' }] },
    ]);
    expect(screen.getByRole('button', { name: 'Виправити' })).toHaveFocus();
  });

  it('a segment edit sends its segment_id and the text trimmed, in one field of at most 2000 characters', async () => {
    const installed = installCorrectable(segmentStored());
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const field = screen.getByRole('textbox', { name: 'Текст фрагмента' });
    expect(field).toHaveValue('Перший текст');
    expect(field).toHaveAttribute('maxlength', '2000');
    fireEvent.change(field, { target: { value: '  Новий текст  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    expect(await screen.findByTestId('transcript-revision')).toHaveTextContent('Ревізія 2');
    expect(paramsOf(installed, 'transcripts.correct')).toEqual([
      { id: REV_NEW, edits: [{ segment_id: 'seg-0', text: 'Новий текст' }] },
    ]);
  });

  it('keeps Save disabled with its reason when nothing changed or the field is empty after trimming', async () => {
    installCorrectable(segmentStored('Текст'));
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const save = screen.getByRole('button', { name: 'Зберегти' });
    const field = screen.getByRole('textbox', { name: 'Текст фрагмента' });
    expect(save).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent('Немає змін');
    expect(save).toHaveAccessibleDescription('Немає змін: змініть текст, щоб зберегти виправлення.');

    fireEvent.change(field, { target: { value: '   ' } });
    expect(save).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent(
      'Текст фрагмента не може бути порожнім',
    );

    fireEvent.change(field, { target: { value: '  Текст  ' } });
    expect(save).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent('Немає змін');

    fireEvent.change(field, { target: { value: 'Інший' } });
    expect(save).toBeEnabled();
    expect(screen.queryByTestId('correction-reason')).not.toBeInTheDocument();
  });

  it('gives the same two reasons for a word: an empty word, and a word that is unchanged after trimming', async () => {
    installCorrectable(wordStored());
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const first = screen.getByRole('textbox', { name: 'Слово 1' });
    expect(screen.getByRole('button', { name: 'Зберегти' })).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent('Немає змін');

    fireEvent.change(first, { target: { value: '  ' } });
    expect(screen.getByRole('button', { name: 'Зберегти' })).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent('Слово не може бути порожнім.');

    fireEvent.change(first, { target: { value: ' привіт ' } });
    expect(screen.getByRole('button', { name: 'Зберегти' })).toBeDisabled();
    expect(screen.getByTestId('correction-reason')).toHaveTextContent('Немає змін');
  });

  it('shows a failed save, a 409 included, under the editor with the backend message, and keeps the typed text', async () => {
    installCorrectable(segmentStored(), {
      correct: () => failure('TRANSCRIPT_REVISION_STALE', 'Ця ревізія вже не остання. Оновіть транскрипцію'),
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Мій варіант' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    const alert = await within(screen.getByTestId('transcript-correction')).findByRole('alert');
    expect(alert).toHaveTextContent('Ця ревізія вже не остання. Оновіть транскрипцію');
    expect(screen.getByRole('textbox', { name: 'Текст фрагмента' })).toHaveValue('Мій варіант');
    expect(screen.getByRole('textbox', { name: 'Текст фрагмента' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Зберегти' })).toBeEnabled();
    expect(screen.getByTestId('transcript-revision')).toHaveTextContent('Ревізія 1');
  });

  it('disables the editor while the save is in flight, and keeps the old revision on screen until the new one arrives', async () => {
    let release = (): void => undefined;
    const hold = new Promise<void>((resolve) => {
      release = () => {
        resolve();
      };
    });
    const installed = installCorrectable(segmentStored('Старий текст'), { holdReads: hold });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Новий текст' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    // The save is stored and the new revision is being read: the old one stays, nothing flashes.
    await waitFor(() => {
      expect(paramsOf(installed, 'transcripts.list')).toHaveLength(2);
    });
    expect(screen.getByTestId('transcript-revision')).toHaveTextContent('Ревізія 1');
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent('Старий текст');
    expect(screen.queryByTestId('transcript-loading')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Текст фрагмента' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Зберегти' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Скасувати' })).toBeDisabled();

    act(() => {
      release();
    });
    expect(await screen.findByTestId('correction-saved')).toHaveTextContent('ревізія 2');
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent('Новий текст');
  });

  it('closes with «Скасувати» or Escape without a save, and returns the focus to «Виправити»', async () => {
    const installed = installCorrectable(segmentStored('Текст'));
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Змінено' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Скасувати' }));
    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Виправити' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Виправити' }));
    const field = screen.getByRole('textbox', { name: 'Текст фрагмента' });
    expect(field).toHaveValue('Текст');
    fireEvent.keyDown(field, { key: 'Escape' });
    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Виправити' })).toHaveFocus();

    expect(paramsOf(installed, 'transcripts.correct')).toEqual([]);
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent('Текст');
  });

  it('keeps one editor open at a time: the other «Виправити» buttons are disabled meanwhile', async () => {
    installCorrectable(twoSegmentsStored());
    renderPanel();

    const buttons = await screen.findAllByRole('button', { name: 'Виправити' });
    expect(buttons).toHaveLength(2);
    fireEvent.click(nth(buttons, 0));
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Виправити' })).toBeDisabled();
  });

  it('reads the words of every window, so a word that is on a later window can be corrected', async () => {
    const stored: Stored = {
      revision: revisionOf(REV_NEW, 1, { timing_level: 'word', segment_count: 2, word_count: 3 }),
      segments: [wordSegmentOf(0, 'а б', ['w-1', 'w-2']), wordSegmentOf(1, 'в', ['w-3'])],
      words: [wordOf('w-1', 0, 'а'), wordOf('w-2', 1, 'б'), wordOf('w-3', 2, 'в')],
    };
    const installed = installCorrectable(stored, { size: 1 });
    renderPanel();

    const buttons = await screen.findAllByRole('button', { name: 'Виправити' });
    expect(buttons).toHaveLength(2);
    expect(paramsOf(installed, 'transcripts.get')).toHaveLength(3);
    fireEvent.click(nth(buttons, 1));
    expect(screen.getByRole('textbox', { name: 'Слово 1' })).toHaveValue('в');
  });

  it('after a failed save the focus goes back to the field, and a failure that is not stale offers no refresh', async () => {
    installCorrectable(segmentStored(), {
      correct: () => failure('INTERNAL_ERROR', 'Не вдалося зберегти. Спробуйте ще раз', true),
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const field = screen.getByRole('textbox', { name: 'Текст фрагмента' });
    fireEvent.change(field, { target: { value: 'Мій варіант' } });
    dropFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    const editor = screen.getByTestId('transcript-correction');
    expect(await within(editor).findByRole('alert')).toHaveTextContent('Не вдалося зберегти');
    expect(field).toHaveFocus();
    expect(within(editor).queryByRole('button', { name: 'Оновити транскрипт' })).toBeNull();
  });

  it('after a failed word save the focus goes back to the first word', async () => {
    installCorrectable(wordStored(), {
      correct: () => failure('INTERNAL_ERROR', 'Не вдалося зберегти. Спробуйте ще раз', true),
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Слово 2' }), { target: { value: 'Світе' } });
    dropFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    expect(await screen.findByTestId('correction-error')).toHaveTextContent('Не вдалося зберегти');
    expect(screen.getByRole('textbox', { name: 'Слово 1' })).toHaveFocus();
  });

  it('an Escape that ends an IME composition does not close the editor; the next Escape does', async () => {
    installCorrectable(segmentStored('Текст'));
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    const field = screen.getByRole('textbox', { name: 'Текст фрагмента' });
    fireEvent.keyDown(field, { key: 'Escape', isComposing: true });
    expect(screen.getByTestId('transcript-correction')).toBeInTheDocument();

    fireEvent.keyDown(field, { key: 'Escape' });
    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
  });

  it('a stale save keeps its message and offers «Оновити транскрипт», which closes the editor and reads the newest revision', async () => {
    const installed = installCorrectable(segmentStored('Перший текст'), {
      correct: () => failure('TRANSCRIPT_REVISION_STALE', 'Ця ревізія вже не остання. Оновіть транскрипцію'),
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Мій варіант' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    const editor = screen.getByTestId('transcript-correction');
    expect(await within(editor).findByRole('alert')).toHaveTextContent(
      'Ця ревізія вже не остання. Оновіть транскрипцію',
    );
    fireEvent.click(within(editor).getByRole('button', { name: 'Оновити транскрипт' }));

    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(paramsOf(installed, 'transcripts.list')).toHaveLength(2);
    });
    expect(await screen.findByTestId('transcript-revision')).toHaveTextContent('Ревізія 1');
    expect(screen.getByTestId('transcript-segment')).toHaveTextContent('Перший текст');
    expect(screen.getByRole('button', { name: 'Виправити' })).toHaveFocus();
    expect(paramsOf(installed, 'transcripts.correct')).toHaveLength(1);
  });

  it('shows the error state with a retry when the newest revision cannot be read after a save', async () => {
    const stored = segmentStored('Старий текст');
    let saved = false;
    installBridge({
      handlers: {
        'transcripts.list': () =>
          saved
            ? failure('INTERNAL_ERROR', 'Внутрішня помилка', true)
            : ok({ items: [stored.revision], next_cursor: null }),
        'transcripts.get': () => ok(windowOfStored(stored, 0, 1000, 1000)),
        'transcripts.correct': (params) => {
          saved = true;
          return ok(correctedOf(stored, params.edits).revision);
        },
      },
    });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Новий текст' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    const error = await screen.findByTestId('transcript-error');
    expect(error).toHaveTextContent('Внутрішня помилка');
    expect(within(error).getByRole('button', { name: 'Спробувати ще раз' })).toBeInTheDocument();
    expect(screen.queryByTestId('transcript-segments')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcript-correction')).not.toBeInTheDocument();
  });

  it('drops the newest revision that a refresh reads after the source changed', async () => {
    let stored: Stored = segmentStored('Текст першого джерела');
    const otherRevision = revisionOf(REV_OLD, 1);
    let saved = false;
    let releaseRead = (): void => undefined;
    const readHeld = new Promise<void>((resolve) => {
      releaseRead = () => {
        resolve();
      };
    });
    const installed = installBridge({
      handlers: {
        'transcripts.list': async (params) => {
          if (params.source_id !== SOURCE_ID) {
            return ok({ items: [otherRevision], next_cursor: null });
          }
          if (saved) {
            // The refresh of the first source is on its way, and its answer comes late.
            await readHeld;
          }
          return ok({ items: [stored.revision], next_cursor: null });
        },
        'transcripts.get': (params) =>
          params.id === REV_OLD
            ? ok(windowOf(otherRevision, [segmentOf(0, 'текст другого джерела', 0)], 0, null))
            : ok(windowOfStored(stored, params.offset ?? 0, params.limit ?? 1000, 1000)),
        'transcripts.correct': (params) => {
          stored = correctedOf(stored, params.edits);
          saved = true;
          return ok(stored.revision);
        },
      },
    });
    const { rerender, onSeek } = renderPanel(SOURCE_ID);

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Виправлено' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));
    await waitFor(() => {
      expect(paramsOf(installed, 'transcripts.list')).toHaveLength(2);
    });

    rerender(<TranscriptPanel sourceId={OTHER_SOURCE_ID} onSeek={onSeek} />);
    expect(await screen.findByText('текст другого джерела')).toBeInTheDocument();

    await act(async () => {
      releaseRead();
      await flushAnswers();
    });
    expect(screen.getByText('текст другого джерела')).toBeInTheDocument();
    expect(screen.queryByText('Виправлено')).not.toBeInTheDocument();
    expect(screen.queryByTestId('correction-saved')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.get')).not.toContainEqual(expect.objectContaining({ id: CORRECTED_ID }));
  });

  it('a save answered after the switch to another source does not read the first one into the second', async () => {
    let stored: Stored = segmentStored('Текст першого джерела');
    const otherRevision = revisionOf(REV_OLD, 1);
    let releaseAnswer = (): void => undefined;
    const answerHeld = new Promise<void>((resolve) => {
      releaseAnswer = () => {
        resolve();
      };
    });
    const installed = installBridge({
      handlers: {
        'transcripts.list': (params) =>
          ok({ items: [params.source_id === SOURCE_ID ? stored.revision : otherRevision], next_cursor: null }),
        'transcripts.get': (params) =>
          params.id === REV_OLD
            ? ok(windowOf(otherRevision, [segmentOf(0, 'текст другого джерела', 0)], 0, null))
            : ok(windowOfStored(stored, params.offset ?? 0, params.limit ?? 1000, 1000)),
        'transcripts.correct': async (params) => {
          stored = correctedOf(stored, params.edits);
          await answerHeld;
          return ok(stored.revision);
        },
      },
    });
    const { rerender, onSeek } = renderPanel(SOURCE_ID);

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Виправлено' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));
    await waitFor(() => {
      expect(paramsOf(installed, 'transcripts.correct')).toHaveLength(1);
    });

    // The save is still out when the panel shows the other source.
    rerender(<TranscriptPanel sourceId={OTHER_SOURCE_ID} onSeek={onSeek} />);
    expect(await screen.findByText('текст другого джерела')).toBeInTheDocument();

    // The answer of the save comes now: the first source is not read again, and the second one stays shown.
    await act(async () => {
      releaseAnswer();
      await flushAnswers();
    });
    expect(screen.getByText('текст другого джерела')).toBeInTheDocument();
    expect(screen.queryByTestId('transcript-loading')).not.toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.list')).toEqual([
      { source_id: SOURCE_ID },
      { source_id: OTHER_SOURCE_ID },
    ]);
  });

  it('a save answered after the panel was remounted with a new key reads nothing from the old one', async () => {
    let stored: Stored = segmentStored('Старий текст');
    let releaseSave = (): void => undefined;
    const saveHeld = new Promise<void>((resolve) => {
      releaseSave = () => {
        resolve();
      };
    });
    const installed = installBridge({
      handlers: {
        'transcripts.list': () => ok({ items: [stored.revision], next_cursor: null }),
        'transcripts.get': (params) => ok(windowOfStored(stored, params.offset ?? 0, params.limit ?? 1000, 1000)),
        'transcripts.correct': async (params) => {
          stored = correctedOf(stored, params.edits);
          await saveHeld;
          return ok(stored.revision);
        },
      },
    });
    const { rerender, onSeek } = renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: 'Виправити' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'Виправлено' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));
    await waitFor(() => {
      expect(paramsOf(installed, 'transcripts.correct')).toHaveLength(1);
    });

    // The panel is remounted with a new key for the same source while the save is out: the old one is gone.
    rerender(<TranscriptPanel key="remounted" sourceId={SOURCE_ID} onSeek={onSeek} />);
    expect(await screen.findByText('Виправлено')).toBeInTheDocument();
    expect(paramsOf(installed, 'transcripts.list')).toHaveLength(2);

    // The save is answered now: the old panel must not read the revisions again.
    await act(async () => {
      releaseSave();
      await flushAnswers();
    });
    expect(paramsOf(installed, 'transcripts.list')).toHaveLength(2);
  });

  it('during an active search, a saved correction leaves the list where the reader scrolled it', async () => {
    installCorrectable(
      segmentsStored(Array.from({ length: 10 }, (_, index) => (index === 5 ? 'ціль' : `рядок ${String(index)}`))),
    );
    renderPanel();
    const search = await screen.findByRole('searchbox', { name: 'Пошук у тексті' });
    const list = layOutList();

    fireEvent.change(search, { target: { value: 'ціль' } });
    // Row 5 starts 200 px down: the list is centred on it at 160 px, as in the search tests.
    expect(list.scrollTop).toBe(160);

    // The reader scrolls the list away from the match, then corrects a row that has no match in it.
    list.scrollTop = 40;
    fireEvent.click(nth(screen.getAllByRole('button', { name: 'Виправити' }), 0));
    fireEvent.change(screen.getByRole('textbox', { name: 'Текст фрагмента' }), {
      target: { value: 'рядок нуль' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Зберегти' }));

    expect(await screen.findByTestId('correction-saved')).toHaveTextContent('ревізія 2');
    expect(screen.getByTestId('transcript-count')).toHaveTextContent('1 із 1');
    expect(list.scrollTop).toBe(40);
  });
});
