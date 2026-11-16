import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ExportBatchDto, ExportItemDto, JobDto } from '../shared/api';
import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { ViewState } from '../shared/bridge';
import { App } from './App';
import {
  CLIP_ID,
  FOLDER_TOKEN,
  PROJECT_ID,
  installBridge,
  listOf,
  makeClip,
  makeProject,
  makeSettings,
  makeSource,
  ok,
} from './test-bridge';
import type { InstalledBridge } from './test-bridge';

// «Налаштування» covers the open project without closing it: the project is mounted but hidden, so
// that it returns with its tab, its export run and its clip selection, and it is inert meanwhile.

const BATCH_ID = '4a6c8e0b-2d3f-4b5c-9d7e-8f0a1b2c3d4e';
const JOB_ID = '2e4a6c8b-1d3f-4a5b-8c7d-9e0f1a2b3c4e';
const CLIPS_STATE: ViewState = { project_id: PROJECT_ID, tab: 'clips', layout: DEFAULT_LAYOUT };

function stubPlay() {
  return vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
}

function stubPause() {
  return vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
}

let media: {
  play: ReturnType<typeof stubPlay>;
  pause: ReturnType<typeof stubPause>;
};

beforeEach(() => {
  // jsdom does not play media: these are the calls the player is expected to make.
  media = { play: stubPlay(), pause: stubPause() };
});

const ITEM: ExportItemDto = {
  id: '8f0a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b2c',
  clip_id: CLIP_ID,
  clip_revision: 2,
  state: 'queued',
  final_path: null,
  duration_ms: null,
  error: null,
};

const BATCH: ExportBatchDto = {
  id: BATCH_ID,
  project_id: PROJECT_ID,
  job_id: JOB_ID,
  status: 'queued',
  output_dir: 'C:\\Exports\\Нарізка',
  items: [ITEM],
  manifest_path: null,
  catalog_path: null,
  created_at: '2026-11-14T10:10:00Z',
};

const JOB: JobDto = { id: JOB_ID, type: 'export', state: 'queued', progress: {}, error: null };

/** Restores the project on «Моменти» with one clip; the clip is `suggested` unless it is given. */
async function openRestored(clip = makeClip()): Promise<InstalledBridge> {
  const installed = installBridge({
    viewState: CLIPS_STATE,
    chooseExportFolder: () => ({ token: FOLDER_TOKEN, displayName: 'Нарізка' }),
    handlers: {
      'projects.list': listOf(makeProject()),
      'sources.list': () => ok({ items: [makeSource()], next_cursor: null }),
      'clips.list': () => ok({ items: [clip], next_cursor: null }),
      'clips.review': (params) =>
        ok({ ...clip, revision: clip.revision + 1, review_status: params.status }),
      'exports.create': () => ok({ id: BATCH_ID, job_id: JOB_ID, status: 'queued' }),
      'exports.get': () => ok(BATCH),
      'jobs.get': () => ok(JOB),
      'settings.get': () => ok(makeSettings()),
    },
  });
  render(<App />);
  await screen.findByTestId('clip-card');
  return installed;
}

function nav(): HTMLElement {
  return screen.getByRole('navigation', { name: 'Основна навігація' });
}

async function openSettings(): Promise<void> {
  fireEvent.click(within(nav()).getByRole('button', { name: 'Налаштування' }));
  await screen.findByTestId('settings-ai');
}

function leaveSettings(): void {
  fireEvent.click(within(nav()).getByRole('button', { name: 'Проєкти' }));
}

function callsOf(installed: InstalledBridge, op: string): number {
  return installed.call.mock.calls.filter(([called]) => called === op).length;
}

/** A key press on the page itself, the way a user's shortcut arrives. */
function press(key: string): boolean {
  return fireEvent.keyDown(document.body, {
    key,
    code: key === ' ' ? 'Space' : `Key${key.toUpperCase()}`,
  });
}

describe('settings over an open project', () => {
  it('keeps the project mounted but hidden while the settings are open', async () => {
    const installed = await openRestored();

    await openSettings();

    const pane = screen.getByTestId('project-pane');
    expect(pane).toHaveAttribute('hidden');
    expect(pane).not.toBeVisible();
    // Mounted: its tabs are in the document, only not available to the user.
    expect(within(pane).getByRole('tab', { name: 'Моменти', hidden: true })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Моменти' })).not.toBeInTheDocument();
    expect(screen.getByTestId('settings-screen')).toBeVisible();
    // It was not read again, nor forgotten.
    expect(callsOf(installed, 'sources.list')).toBe(1);

    leaveSettings();

    expect(screen.getByTestId('project-pane')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('project-pane')).toBeVisible();
    expect(screen.queryByTestId('settings-screen')).not.toBeInTheDocument();
  });

  it('returns with the tab, the export run and the clip selection it had', async () => {
    const approved = makeClip({ review_status: 'approved', revision: 2 });
    const installed = await openRestored(approved);
    // Tick the clip and export it: the «Експорт» tab opens with the batch.
    fireEvent.click(within(screen.getByTestId('clip-card')).getByTestId('select-clip'));
    fireEvent.click(screen.getByTestId('export-selected'));
    fireEvent.click(await screen.findByTestId('confirm-export'));
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'Експорт' })).toHaveAttribute('aria-selected', 'true');
    });
    // Back to «Моменти»: the clip is still ticked, and the batch is still there to open.
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('clip-card');
    expect(within(screen.getByTestId('clip-card')).getByTestId('select-clip')).toBeChecked();
    const reads = {
      sources: callsOf(installed, 'sources.list'),
      clips: callsOf(installed, 'clips.list'),
      created: callsOf(installed, 'exports.create'),
    };
    installed.setViewState.mockClear();

    await openSettings();
    leaveSettings();

    expect(screen.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Експорт' })).toBeEnabled();
    expect(within(screen.getByTestId('clip-card')).getByTestId('select-clip')).toBeChecked();
    expect(screen.getByTestId('export-counts')).toHaveTextContent('Вибрано 1');
    expect({
      sources: callsOf(installed, 'sources.list'),
      clips: callsOf(installed, 'clips.list'),
      created: callsOf(installed, 'exports.create'),
    }).toEqual(reads);
    expect(installed.setViewState).not.toHaveBeenCalled();
    // The export run is the one that was started: its tab shows that batch.
    fireEvent.click(screen.getByRole('tab', { name: 'Експорт' }));
    expect(await screen.findByRole('tabpanel', { name: 'Експорт' })).toBeVisible();
  });

  it('keeps the project list out of it: the list reads its data again, as before', async () => {
    const installed = installBridge({
      handlers: { 'projects.list': listOf(makeProject()), 'settings.get': () => ok(makeSettings()) },
    });
    render(<App />);
    await screen.findByTestId('project-item');

    await openSettings();
    leaveSettings();

    expect(await screen.findByTestId('project-item')).toBeVisible();
    expect(screen.queryByTestId('project-pane')).not.toBeInTheDocument();
    expect(callsOf(installed, 'projects.list')).toBe(2);
  });

  it('pauses the player when the settings open', async () => {
    await openRestored();
    media.pause.mockClear();

    await openSettings();

    expect(media.pause).toHaveBeenCalled();
  });

  it('ignores the keyboard shortcuts of the hidden project, and gets them back on return', async () => {
    const installed = await openRestored();
    // The clip being worked on and a loaded video: the shortcuts have something to act on.
    fireEvent.click(within(screen.getByTestId('clip-card')).getByTestId('open-clip'));
    const video = screen.getByTestId('player-video');
    Object.defineProperty(video, 'currentTime', { configurable: true, writable: true, value: 0 });
    fireEvent.loadedMetadata(video);

    await openSettings();
    expect(press('a')).toBe(true);
    expect(press(' ')).toBe(true);

    // `true`: nobody cancelled the key, so nothing acted on it.
    expect(callsOf(installed, 'clips.review')).toBe(0);
    expect(media.play).not.toHaveBeenCalled();

    leaveSettings();
    expect(press(' ')).toBe(false);
    expect(media.play).toHaveBeenCalledTimes(1);
    expect(press('a')).toBe(false);
    await waitFor(() => {
      expect(callsOf(installed, 'clips.review')).toBe(1);
    });
  });
});
