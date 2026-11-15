import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ClipDto } from '../shared/api';
import { DEFAULT_LAYOUT } from '../shared/bridge';
import type { PanelLayout } from '../shared/bridge';
import { App } from './App';
import {
  PROJECT_ID,
  failure,
  installBridge,
  installMediaQuery,
  listOf,
  makeClip,
  makeProject,
  makeSource,
  ok,
} from './test-bridge';
import type { ApiHandlers, InstalledBridge, MediaControl } from './test-bridge';

// The Моменти workspace (docs/03 sections 2 and 8): three columns from 1440 px, tabs under the
// player below it, splitters that resize the panels and are remembered. jsdom does no layout,
// so what is checked here is the structure, the semantics and what is saved; the pixels are
// checked in a real window by apps/desktop/e2e/layout.spec.mjs.

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
});

function earlierRun(clips: readonly ClipDto[] = [makeClip()]): ApiHandlers {
  return {
    'projects.list': listOf(makeProject()),
    'sources.list': () => ok({ items: [makeSource()], next_cursor: null }),
    'clips.list': () => ok({ items: [...clips], next_cursor: null }),
  };
}

interface Opened {
  readonly installed: InstalledBridge;
  readonly media: MediaControl | null;
}

/** The app reopened on «Моменти» of the project (a restart), with the media query answering `wide`. */
async function open(
  options: { readonly wide?: boolean; readonly layout?: PanelLayout; readonly handlers?: ApiHandlers } = {},
): Promise<Opened> {
  const media = options.wide === undefined ? null : installMediaQuery(options.wide);
  const installed = installBridge({
    handlers: options.handlers ?? earlierRun(),
    viewState: { project_id: PROJECT_ID, tab: 'clips', layout: options.layout ?? DEFAULT_LAYOUT },
  });
  render(<App />);
  await screen.findByTestId('player');
  return { installed, media };
}

function listSplitter(): HTMLElement {
  return screen.getByRole('separator', { name: 'Ширина списку фрагментів' });
}

function detailsSplitter(): HTMLElement {
  return screen.getByRole('separator', { name: 'Ширина панелі фрагмента' });
}

function grid(): HTMLElement {
  return document.querySelector<HTMLElement>('.moments-grid') ?? failMissing('the moments grid');
}

function failMissing(what: string): never {
  throw new Error(`no ${what}`);
}

function remembered(installed: InstalledBridge) {
  return installed.setViewState.mock.calls.map(([state]) => state);
}

describe('three columns from 1440 px', () => {
  it('shows the list, the player and the right panel side by side, with a splitter on each side of the player', async () => {
    await open({ wide: true });

    expect(grid()).toHaveAttribute('data-mode', 'wide');
    expect(screen.getAllByRole('separator')).toHaveLength(2);
    expect(listSplitter()).toBeVisible();
    expect(detailsSplitter()).toBeVisible();
    expect(screen.getByTestId('details-panel')).toHaveAttribute('data-tabbed', 'false');
    // No tabs: the editor and the shortcuts are both there.
    expect(screen.queryByRole('tablist', { name: 'Вкладки панелі фрагмента' })).not.toBeInTheDocument();
    expect(screen.getByTestId('editor-empty')).toBeVisible();
    expect(screen.getByTestId('hotkeys-hint')).toBeVisible();
  });

  it('is also what a page without media queries gets', async () => {
    await open();

    expect(grid()).toHaveAttribute('data-mode', 'wide');
    expect(screen.getAllByRole('separator')).toHaveLength(2);
  });

  it('puts the editor of the active clip in the right panel', async () => {
    await open({ wide: true });

    fireEvent.click(await screen.findByTestId('open-clip'));

    const panel = screen.getByTestId('details-panel');
    expect(within(panel).getByTestId('clip-editor')).toBeVisible();
    expect(within(panel).getByTestId('clip-title')).toHaveValue('Мій фрагмент');
    expect(screen.queryByTestId('editor-empty')).not.toBeInTheDocument();
  });

  it('points each splitter at the panel it resizes', async () => {
    await open({ wide: true });

    const list = document.getElementById(listSplitter().getAttribute('aria-controls') ?? '');
    const details = document.getElementById(detailsSplitter().getAttribute('aria-controls') ?? '');

    expect(list).toBe(screen.getByRole('region', { name: 'Фрагменти' }));
    expect(details).toBe(screen.getByTestId('details-panel'));
  });
});

describe('tabs under the player below 1440 px', () => {
  it('keeps one splitter, for the list, and turns the right panel into two tabs', async () => {
    await open({ wide: false });

    expect(grid()).toHaveAttribute('data-mode', 'narrow');
    expect(screen.getAllByRole('separator')).toHaveLength(1);
    expect(listSplitter()).toBeVisible();
    expect(screen.queryByRole('separator', { name: 'Ширина панелі фрагмента' })).not.toBeInTheDocument();
    expect(screen.getByTestId('details-panel')).toHaveAttribute('data-tabbed', 'true');
    const tabs = within(screen.getByRole('tablist', { name: 'Вкладки панелі фрагмента' })).getAllByRole('tab');
    expect(tabs.map((tab) => tab.textContent)).toEqual(['Межі', 'Клавіші']);
    expect(tabs.map((tab) => tab.getAttribute('aria-selected'))).toEqual(['true', 'false']);
  });

  it('shows the editor first and the shortcuts on the second tab', async () => {
    await open({ wide: false });

    expect(screen.getByRole('tabpanel', { name: 'Межі' })).toBeVisible();
    expect(screen.getByTestId('editor-empty')).toBeVisible();
    expect(screen.getByTestId('hotkeys-hint')).not.toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: 'Клавіші' }));

    expect(screen.getByRole('tab', { name: 'Клавіші' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tabpanel', { name: 'Клавіші' })).toBeVisible();
    expect(screen.getByTestId('hotkeys-hint')).toBeVisible();
    expect(screen.getByTestId('editor-empty')).not.toBeVisible();
  });

  it('goes back to the fields when a clip is opened or a new one is started', async () => {
    await open({ wide: false });

    fireEvent.click(screen.getByRole('tab', { name: 'Клавіші' }));
    fireEvent.click(await screen.findByTestId('open-clip'));
    expect(screen.getByRole('tab', { name: 'Межі' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('clip-editor')).toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: 'Клавіші' }));
    fireEvent.click(screen.getByTestId('manual-clip'));
    expect(screen.getByRole('tab', { name: 'Межі' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('clip-title')).toBeVisible();
    expect(screen.getByTestId('clip-title')).toHaveFocus();
  });

  it('keeps what was typed when the other tab is opened and closed again', async () => {
    await open({ wide: false });
    fireEvent.click(screen.getByTestId('manual-clip'));
    fireEvent.change(screen.getByTestId('clip-title'), { target: { value: 'Чернетка' } });
    fireEvent.change(screen.getByTestId('clip-start'), { target: { value: '00:00:01.000' } });

    fireEvent.click(screen.getByRole('tab', { name: 'Клавіші' }));
    expect(screen.getByTestId('clip-title')).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Межі' }));

    expect(screen.getByTestId('clip-title')).toHaveValue('Чернетка');
    expect(screen.getByTestId('clip-start')).toHaveValue('00:00:01.000');
  });

  it('keeps the shortcuts hint reachable: its tab is a real button in the tab order', async () => {
    await open({ wide: false });

    const tab = screen.getByRole('tab', { name: 'Клавіші' });

    expect(tab.tagName).toBe('BUTTON');
    expect(tab).toBeEnabled();
    const panel = document.getElementById(tab.getAttribute('aria-controls') ?? '');
    expect(panel).toHaveAttribute('role', 'tabpanel');
    expect(panel).toHaveAttribute('aria-labelledby', tab.id);
    expect(panel).toContainElement(screen.getByTestId('hotkeys-hint'));
  });
});

describe('crossing 1440 px', () => {
  it('does not remount the new-clip form: the text typed in it is still there', async () => {
    const { media } = await open({ wide: false });
    fireEvent.click(screen.getByTestId('manual-clip'));
    fireEvent.change(screen.getByTestId('clip-title'), { target: { value: 'Чернетка' } });
    const field = screen.getByTestId('clip-title');

    media?.setWide(true);

    expect(grid()).toHaveAttribute('data-mode', 'wide');
    expect(screen.getAllByRole('separator')).toHaveLength(2);
    expect(screen.getByTestId('clip-title')).toBe(field);
    expect(field).toHaveValue('Чернетка');

    media?.setWide(false);

    expect(grid()).toHaveAttribute('data-mode', 'narrow');
    expect(screen.getByTestId('clip-title')).toBe(field);
    expect(field).toHaveValue('Чернетка');
  });

  it('keeps the clip that is open and its editor', async () => {
    const { media } = await open({ wide: true });
    fireEvent.click(await screen.findByTestId('open-clip'));
    const editor = screen.getByTestId('clip-editor');

    media?.setWide(false);

    expect(screen.getByTestId('clip-editor')).toBe(editor);
    expect(screen.getByRole('tabpanel', { name: 'Межі' })).toContainElement(editor);
  });
});

describe('the splitters', () => {
  it('start at the remembered widths', async () => {
    await open({ wide: true, layout: { list_px: 260, details_px: 400 } });

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '260');
    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '400');
    expect(grid().style.getPropertyValue('--list-w')).toBe('260px');
    expect(grid().style.getPropertyValue('--details-w')).toBe('400px');
  });

  it('start at ~320 px each when nothing is remembered', async () => {
    await open({ wide: true });

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '320');
    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '320');
    expect(listSplitter()).toHaveAttribute('aria-valuemin', '240');
    expect(listSplitter()).toHaveAttribute('aria-valuemax', '480');
    expect(detailsSplitter()).toHaveAttribute('aria-valuemin', '280');
    expect(detailsSplitter()).toHaveAttribute('aria-valuemax', '480');
  });

  it('write nothing just because the app opened', async () => {
    const { installed } = await open({ wide: true, layout: { list_px: 260, details_px: 400 } });

    expect(installed.setViewState).not.toHaveBeenCalled();
  });

  it('resize the list with the arrow keys, and every press is remembered with the project and tab', async () => {
    const { installed } = await open({ wide: true });

    listSplitter().focus();
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '336');
    expect(grid().style.getPropertyValue('--list-w')).toBe('336px');
    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 336, details_px: 320 },
    });
    expect(await installed.bridge.getViewState()).toEqual({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 336, details_px: 320 },
    });

    fireEvent.keyDown(listSplitter(), { key: 'ArrowLeft', shiftKey: true });
    fireEvent.keyDown(listSplitter(), { key: 'ArrowLeft', shiftKey: true });

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '240');
    expect(remembered(installed).map((state) => state?.layout.list_px)).toEqual([336, 272, 240]);
  });

  it('resize the right panel: the left arrow makes it wider', async () => {
    const { installed } = await open({ wide: true });

    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowLeft' });
    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowLeft', shiftKey: true });

    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '400');
    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 320, details_px: 400 },
    });
  });

  it('never leave their limits, and Home and End go to them', async () => {
    const { installed } = await open({ wide: true });

    fireEvent.keyDown(listSplitter(), { key: 'End' });
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight', shiftKey: true });
    expect(listSplitter()).toHaveAttribute('aria-valuenow', '480');

    fireEvent.keyDown(detailsSplitter(), { key: 'Home' });
    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowRight', shiftKey: true });
    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '280');

    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 480, details_px: 280 },
    });
  });

  it('follow the pointer while it is dragged and are remembered once, when it is let go', async () => {
    const { installed } = await open({ wide: true });

    fireEvent.pointerDown(listSplitter(), { button: 0, clientX: 400, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 440, pointerId: 1 });
    expect(listSplitter()).toHaveAttribute('aria-valuenow', '360');
    fireEvent.pointerMove(window, { clientX: 470, pointerId: 1 });
    expect(listSplitter()).toHaveAttribute('aria-valuenow', '390');
    expect(installed.setViewState).not.toHaveBeenCalled();

    fireEvent.pointerUp(window, { clientX: 470, pointerId: 1 });

    expect(installed.setViewState).toHaveBeenCalledTimes(1);
    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 390, details_px: 320 },
    });
  });

  it('keep the widths when the user goes to another tab and comes back', async () => {
    await open({ wide: true });
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });
    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowLeft' });

    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '336');
    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '336');
  });

  it('keep the width of the right panel through a narrow window, where it has no splitter', async () => {
    const { installed, media } = await open({ wide: true });
    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowLeft' });

    media?.setWide(false);
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });
    media?.setWide(true);

    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '336');
    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 336, details_px: 336 },
    });
  });

  it('remember the tab that is open, not the one the project started on', async () => {
    const { installed } = await open({ wide: true });
    fireEvent.click(screen.getByRole('tab', { name: 'Джерела' }));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByRole('tab', { name: 'Моменти' }));
    await screen.findByTestId('player');

    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });

    expect(installed.setViewState).toHaveBeenLastCalledWith({
      project_id: PROJECT_ID,
      tab: 'clips',
      layout: { list_px: 336, details_px: 320 },
    });
  });

  it('come back after a restart exactly as they were left', async () => {
    const first = await open({ wide: true });
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight', shiftKey: true });
    fireEvent.keyDown(detailsSplitter(), { key: 'ArrowRight', shiftKey: true });
    const left = await first.installed.bridge.getViewState();
    expect(left).toEqual({ project_id: PROJECT_ID, tab: 'clips', layout: { list_px: 384, details_px: 280 } });
    document.body.replaceChildren();

    const second = installBridge({ handlers: earlierRun(), viewState: left });
    render(<App />);
    await screen.findByTestId('player');

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '384');
    expect(detailsSplitter()).toHaveAttribute('aria-valuenow', '280');
    expect(second.setViewState).not.toHaveBeenCalled();
  });

  it('go on working when the widths cannot be saved', async () => {
    const { installed } = await open({ wide: true });
    installed.setViewState.mockRejectedValue(new Error('IPC refused'));

    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });

    expect(listSplitter()).toHaveAttribute('aria-valuenow', '336');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('do not break the review shortcuts: arrows on a splitter are not seek keys', async () => {
    await open({ wide: true });
    const seek = vi.spyOn(HTMLMediaElement.prototype, 'currentTime', 'set');

    listSplitter().focus();
    fireEvent.keyDown(listSplitter(), { key: 'ArrowRight' });

    expect(seek).not.toHaveBeenCalled();
  });
});

describe('the player', () => {
  it('takes the proportions of the picture once the video knows them', async () => {
    await open({ wide: true });
    const video = screen.getByTestId('player-video');
    Object.defineProperty(video, 'videoWidth', { value: 1080, configurable: true });
    Object.defineProperty(video, 'videoHeight', { value: 1920, configurable: true });

    expect(screen.getByTestId('player-stage')).not.toHaveAttribute('style');
    fireEvent.loadedMetadata(video);

    expect(screen.getByTestId('player-stage').style.aspectRatio).toBe('1080 / 1920');
  });

  it('stays 16:9 (the stylesheet default) while the size is unknown', async () => {
    await open({ wide: true });
    fireEvent.loadedMetadata(screen.getByTestId('player-video'));

    expect(screen.getByTestId('player-stage')).not.toHaveAttribute('style');
  });
});

describe('the list when the clips cannot be read', () => {
  it('still has its splitter, so the layout does not jump when the error goes away', async () => {
    await open({
      wide: true,
      handlers: { ...earlierRun(), 'clips.list': () => failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true) },
    });

    expect(await screen.findByTestId('clips-error')).toBeVisible();
    expect(listSplitter()).toBeVisible();
    expect(detailsSplitter()).toBeVisible();
  });
});
