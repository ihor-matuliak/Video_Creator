import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ClipDto, ExportBatchDto, ExportItemDto } from '../shared/api';
import { App } from './App';
import {
  CLIP_ID,
  FOLDER_TOKEN,
  PROJECT_ID,
  failure,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, BridgeOptions, InstalledBridge } from './test-bridge';

// docs/03 section 2: "a button that cannot be used has a visible explanation next to it". Every
// state below leaves a primary button disabled; none may do it without saying why.

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
});

const BATCH_ID = '4a6c8e0b-2d3f-4b5c-9d7e-8f0a1b2c3d4e';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const NEVER: Promise<never> = new Promise<never>(() => undefined);

/** The visible text that explains why `button` is off: a note it points at, or a label that says it is busy. */
function reasonOf(button: HTMLElement): string {
  const described = (button.getAttribute('aria-describedby') ?? '').split(/\s+/).filter((id) => id !== '');
  for (const id of described) {
    const note = document.getElementById(id);
    if (note !== null && spoken(note) !== '') {
      expect(note, `the note of ${name(button)} is hidden`).toBeVisible();
      return spoken(note);
    }
  }
  const label = button.textContent.trim();
  return label.endsWith('…') ? label : '';
}

/** The text of an element without its decorative icon (aria-hidden), as a screen reader would say it. */
function spoken(element: HTMLElement): string {
  const copy = element.cloneNode(true) as HTMLElement;
  for (const icon of copy.querySelectorAll('[aria-hidden="true"]')) {
    icon.remove();
  }
  return copy.textContent.trim();
}

function name(button: HTMLElement): string {
  return button.getAttribute('data-testid') ?? button.textContent;
}

/** Every disabled primary button on the screen, each checked for a reason. */
function disabledPrimaries(): HTMLElement[] {
  const buttons = [...document.querySelectorAll<HTMLElement>('button.primary:disabled')];
  for (const button of buttons) {
    expect(reasonOf(button), `${name(button)} is disabled and does not say why`).not.toBe('');
  }
  return buttons;
}

function reasonFor(testId: string): string {
  const button = screen.getByTestId(testId);
  expect(button).toBeDisabled();
  expect(disabledPrimaries()).toContain(button);
  return reasonOf(button);
}

function clipsOf(...clips: ClipDto[]): ApiHandlers {
  const list = [...clips];
  return {
    'clips.list': () => ok({ items: [...list], next_cursor: null }),
    'clips.review': (params) => {
      const current = list.find((clip) => clip.id === params.id);
      return current === undefined
        ? failure('CLIP_NOT_FOUND', 'Кліп не знайдено')
        : ok({ ...current, revision: current.revision + 1, review_status: params.status });
    },
  };
}

async function openProject(
  options: { readonly handlers?: ApiHandlers; readonly bridge?: Partial<BridgeOptions> } = {},
): Promise<InstalledBridge> {
  const installed = installBridge({
    importVideo: () => ok(makeSource()),
    chooseExportFolder: () => ({ token: FOLDER_TOKEN, displayName: 'Нарізка' }),
    ...options.bridge,
    handlers: {
      'projects.list': listOf(makeProject()),
      ...clipsOf(),
      ...options.handlers,
    },
  });
  render(<App />);
  fireEvent.click(await screen.findByTestId('project-item'));
  return installed;
}

async function openMoments(
  options: { readonly handlers?: ApiHandlers; readonly bridge?: Partial<BridgeOptions> } = {},
): Promise<InstalledBridge> {
  const installed = await openProject(options);
  fireEvent.click(await screen.findByTestId('import-video'));
  await screen.findByTestId('source-ready');
  fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
  await screen.findByTestId('player');
  return installed;
}

const approvedClip = makeClip({ review_status: 'approved', revision: 2 });
const pendingClip = makeClip({ review_status: 'suggested' });

describe('the new-clip form', () => {
  it('says what is missing while «Зберегти фрагмент» is off, before any field was touched', async () => {
    await openMoments();
    fireEvent.click(screen.getByTestId('manual-clip'));

    expect(reasonFor('save-clip')).toContain('Початок: введіть час');
    expect(screen.getByTestId('save-clip-reason')).toBeVisible();
  });

  it('follows the first problem as the fields are filled in', async () => {
    await openMoments();
    fireEvent.click(screen.getByTestId('manual-clip'));

    fireEvent.change(screen.getByTestId('clip-start'), { target: { value: '00:00:01.000' } });
    expect(reasonFor('save-clip')).toContain('Кінець: введіть час');

    fireEvent.change(screen.getByTestId('clip-end'), { target: { value: '00:00:00.500' } });
    expect(reasonFor('save-clip')).toContain('Кінець має бути пізніше за початок');

    fireEvent.change(screen.getByTestId('clip-end'), { target: { value: '00:59:00.000' } });
    expect(reasonFor('save-clip')).toContain('Фрагмент має вміщатися у відео');
  });

  it('lets the error under the fields be the reason once it is shown, and does not say it twice', async () => {
    await openMoments();
    fireEvent.click(screen.getByTestId('manual-clip'));
    fireEvent.change(screen.getByTestId('clip-start'), { target: { value: '00:00:08.000' } });
    fireEvent.change(screen.getByTestId('clip-end'), { target: { value: '00:00:04.000' } });
    fireEvent.blur(screen.getByTestId('clip-end'));

    expect(reasonFor('save-clip')).toContain('Кінець має бути пізніше за початок');
    expect(screen.getByTestId('interval-error')).toBeVisible();
    expect(screen.queryByTestId('save-clip-reason')).not.toBeInTheDocument();
  });

  it('needs no note once the interval is valid, and again says «Зберігаємо…» while the clip is stored', async () => {
    await openMoments({ handlers: { 'clips.create': () => NEVER } });
    fireEvent.click(screen.getByTestId('manual-clip'));
    fireEvent.change(screen.getByTestId('clip-start'), { target: { value: '00:00:04.120' } });
    fireEvent.change(screen.getByTestId('clip-end'), { target: { value: '00:00:07.880' } });

    expect(screen.getByTestId('save-clip')).toBeEnabled();
    expect(screen.queryByTestId('save-clip-reason')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('save-clip'));

    expect(reasonFor('save-clip')).toBe('Зберігаємо…');
    expect(screen.queryByTestId('save-clip-reason')).not.toBeInTheDocument();
  });
});

describe('the review buttons of a clip', () => {
  it('say «Зберігаємо рішення…» while the decision is being stored', async () => {
    await openMoments({ handlers: { ...clipsOf(pendingClip), 'clips.review': () => NEVER } });
    const card = await screen.findByTestId('clip-card');

    fireEvent.click(within(card).getByTestId('approve-clip'));

    await waitFor(() => {
      expect(within(card).getByTestId('approve-clip')).toBeDisabled();
    });
    expect(reasonFor('approve-clip')).toBe('Зберігаємо рішення…');
    expect(within(card).getByTestId('reject-clip')).toHaveAccessibleDescription('Зберігаємо рішення…');
  });

  it('are all enabled, with no note, when nothing is being stored', async () => {
    await openMoments({ handlers: clipsOf(pendingClip) });
    await screen.findByTestId('clip-card');

    expect(screen.getByTestId('approve-clip')).toBeEnabled();
    expect(screen.queryByTestId('clip-busy')).not.toBeInTheDocument();
    // Only the export button is off (nothing is selected), and it says so.
    expect(disabledPrimaries().map((button) => button.getAttribute('data-testid'))).toEqual(['export-selected']);
  });
});

describe('the export bar', () => {
  it('says to select clips when none is selected', async () => {
    await openMoments({ handlers: clipsOf(approvedClip) });
    await screen.findByTestId('clip-card');

    expect(reasonFor('export-selected')).toBe('Виберіть фрагменти для експорту');
  });

  it('says to approve when the selected clips are not approved', async () => {
    await openMoments({ handlers: clipsOf(pendingClip) });
    fireEvent.click(await screen.findByTestId('select-clip'));

    expect(reasonFor('export-selected')).toContain('ще не підтверджено');
  });

  it('says that a folder is being chosen while the picker is open', async () => {
    await openMoments({
      handlers: clipsOf(approvedClip),
      bridge: { chooseExportFolder: () => NEVER },
    });
    fireEvent.click(await screen.findByTestId('select-clip'));

    fireEvent.click(screen.getByTestId('export-selected'));

    await waitFor(() => {
      expect(screen.getByTestId('export-selected')).toBeDisabled();
    });
    expect(reasonFor('export-selected')).toBe('Обираємо теку для експорту…');
  });

  it('says that the parameters are open below while the summary is shown', async () => {
    await openMoments({ handlers: clipsOf(approvedClip) });
    fireEvent.click(await screen.findByTestId('select-clip'));
    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');

    expect(reasonFor('export-selected')).toContain('Параметри експорту відкрито нижче');
    expect(screen.getByTestId('confirm-export')).toBeEnabled();
    expect(screen.queryByTestId('export-busy')).not.toBeInTheDocument();
  });

  it('says that the export is starting while «Почати експорт» is busy', async () => {
    await openMoments({
      handlers: { ...clipsOf(approvedClip), 'exports.create': () => NEVER },
    });
    fireEvent.click(await screen.findByTestId('select-clip'));
    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');

    fireEvent.click(screen.getByTestId('confirm-export'));

    expect(screen.getByTestId('confirm-export')).toHaveTextContent('Запускаємо…');
    expect(reasonFor('confirm-export')).toBe('Запускаємо експорт…');
    expect(screen.getByTestId('export-busy')).toHaveTextContent('Запускаємо експорт…');
    expect(reasonFor('export-selected')).toContain('Параметри експорту відкрито нижче');
  });

  it('says that a folder is being chosen again while «Обрати теку ще раз» is busy', async () => {
    let picks = 0;
    await openMoments({
      handlers: { ...clipsOf(approvedClip), 'exports.create': () => failure('FOLDER_TOKEN_USED', 'Теку вже використано') },
      bridge: {
        chooseExportFolder: () => {
          picks += 1;
          return picks === 1 ? { token: FOLDER_TOKEN, displayName: 'Нарізка' } : NEVER;
        },
      },
    });
    fireEvent.click(await screen.findByTestId('select-clip'));
    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');
    fireEvent.click(screen.getByTestId('confirm-export'));
    fireEvent.click(await screen.findByTestId('export-rechoose'));

    await waitFor(() => {
      expect(screen.getByTestId('export-rechoose')).toBeDisabled();
    });
    expect(reasonFor('export-rechoose')).toBe('Обираємо теку для експорту…');
  });
});

describe('the export batch', () => {
  function failedBatch(): ExportBatchDto {
    const item: ExportItemDto = {
      id: '8f0a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b2c',
      clip_id: CLIP_ID,
      clip_revision: 2,
      state: 'failed',
      final_path: null,
      duration_ms: null,
      error: { code: 'FFMPEG_FAILED', message: 'FFmpeg не зміг створити кліп' },
    };
    return {
      id: BATCH_ID,
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      status: 'failed',
      output_dir: 'C:\\Exports\\Нарізка',
      items: [item],
      manifest_path: null,
      catalog_path: null,
      created_at: '2026-11-14T10:10:00Z',
    };
  }

  it('says that the retry is starting while «Повторити невдалі» is busy', async () => {
    await openMoments({
      handlers: {
        ...clipsOf(approvedClip),
        'exports.create': () => ok({ id: BATCH_ID, job_id: JOB_ID, status: 'queued' }),
        'exports.get': () => ok(failedBatch()),
        'jobs.get': () => ok({ id: JOB_ID, type: 'export', state: 'failed', progress: {}, error: null }),
        'exports.retry': () => NEVER,
      },
    });
    fireEvent.click(await screen.findByTestId('select-clip'));
    fireEvent.click(screen.getByTestId('export-selected'));
    await screen.findByTestId('export-summary');
    fireEvent.click(screen.getByTestId('confirm-export'));
    fireEvent.click(await screen.findByTestId('retry-failed'));

    await waitFor(() => {
      expect(screen.getByTestId('retry-failed')).toBeDisabled();
    });
    expect(reasonFor('retry-failed')).toBe('Запускаємо повторний експорт…');
  });
});

describe('the other primary buttons', () => {
  it('«Додати відео» says that the video is being added', async () => {
    await openProject({ bridge: { importVideo: () => NEVER } });

    fireEvent.click(await screen.findByTestId('import-video'));

    expect(reasonFor('import-video')).toContain('Додаємо відео');
  });

  it('«Створити» says that the project is being created', async () => {
    installBridge({
      handlers: { 'projects.list': listOf(), 'projects.create': () => NEVER },
    });
    render(<App />);
    fireEvent.click(await screen.findByTestId('new-project'));
    fireEvent.change(screen.getByTestId('project-name'), { target: { value: 'Новий' } });

    fireEvent.click(screen.getByTestId('create-project'));

    expect(reasonFor('create-project')).toBe('Створюємо…');
  });

  it('no primary button is disabled on the project list or on the sources tab at rest', async () => {
    await openProject();
    await screen.findByTestId('import-video');

    expect(disabledPrimaries()).toEqual([]);
  });
});
