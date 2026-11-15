import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { SourceDto } from '../shared/api';
import type { ImportVideoResult } from '../shared/bridge';
import { App } from './App';
import { PROJECT_ID, failure, installBridge, listOf, makeProject, makeSource, ok } from './test-bridge';
import type { ApiHandlers, BridgeOptions } from './test-bridge';

/** Opens the project «Перевірка інтерфейсу» on its Sources tab, as the E2E beforeEach does. */
async function openSources(
  importVideo: BridgeOptions['importVideo'],
  handlers: ApiHandlers = {},
): Promise<ReturnType<typeof installBridge>> {
  const installed = installBridge({
    importVideo,
    handlers: { 'projects.list': listOf(makeProject()), ...handlers },
  });
  render(<App />);
  fireEvent.click(await screen.findByTestId('project-item'));
  await screen.findByTestId('import-video');
  return installed;
}

function imported(source: SourceDto): ImportVideoResult {
  return ok(source);
}

describe('Sources tab: import', () => {
  it('shows the source inspector with a HH:MM:SS.mmm duration after a successful import', async () => {
    const { importVideo } = await openSources(() => imported(makeSource()));
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('import-video'));

    const ready = await screen.findByTestId('source-ready');
    expect(ready).toBeVisible();
    expect(within(ready).getByRole('heading', { name: 'color_gop.mp4' })).toBeInTheDocument();
    expect(ready).toHaveTextContent('00:00:12.000');
    expect(screen.getByTestId('source-duration')).toHaveTextContent('00:00:12.000');
    expect(importVideo).toHaveBeenCalledExactlyOnceWith({ project_id: PROJECT_ID });
  });

  it('shows availability, hash status and audio tracks with icon and text', async () => {
    await openSources(() => imported(makeSource()));

    fireEvent.click(screen.getByTestId('import-video'));

    await screen.findByTestId('source-ready');
    expect(screen.getByTestId('source-availability')).toHaveTextContent('✓ Доступний');
    expect(screen.getByTestId('source-hash-status')).toHaveTextContent('✓ Підтверджена');
    expect(screen.getByTestId('source-audio')).toHaveTextContent(
      'Доріжка 1 · uk · 2 кан. · AAC · за замовчуванням',
    );
  });

  it('shows resolution, frame rate, VFR badge and the way of viewing after an import', async () => {
    const source = makeSource({
      display_name: 'vfr.mkv',
      video: { width: 1920, height: 1080, frame_rate: '30000/1001', vfr: true },
      playback: { direct: false, reason: 'CONTAINER_UNSUPPORTED', direct_audio_stream_index: null },
    });
    await openSources(() => imported(source));

    fireEvent.click(screen.getByTestId('import-video'));

    await screen.findByTestId('source-ready');
    expect(screen.getByTestId('source-resolution')).toHaveTextContent('1920×1080');
    expect(screen.getByTestId('source-fps')).toHaveTextContent('29,97 к/с');
    expect(screen.getByTestId('source-vfr')).toHaveTextContent('⚠ Змінна частота кадрів (VFR)');
    expect(screen.getByTestId('source-playback')).toHaveTextContent(
      'Потрібна копія для перегляду. Причина: контейнер файлу не підтримується програвачем',
    );
    expect(screen.getByTestId('source-local-note')).toHaveTextContent(
      "Відео залишається на цьому комп'ютері.",
    );
  });

  it('lists every audio track of a source', async () => {
    const source = makeSource({
      audio_streams: [
        { index: 1, codec: 'aac', channels: 2, language: 'uk', title: 'Гра', default: true, start_offset_ms: 0 },
        { index: 2, codec: 'opus', channels: null, language: null, title: 'Мікрофон', default: false, start_offset_ms: 0 },
      ],
    });
    await openSources(() => imported(source));

    fireEvent.click(screen.getByTestId('import-video'));

    const audio = await screen.findByTestId('source-audio');
    expect(within(audio).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Доріжка 1 · uk · Гра · 2 кан. · AAC · за замовчуванням',
      'Доріжка 2 · Мікрофон · OPUS',
    ]);
  });

  it('says that a source without audio can still be clipped by hand', async () => {
    await openSources(() => imported(makeSource({ audio_streams: [] })));

    fireEvent.click(screen.getByTestId('import-video'));

    expect(await screen.findByTestId('source-audio')).toHaveTextContent(
      'У відео немає звукової доріжки. Можна створювати фрагменти вручну',
    );
  });

  it('states that the video stays on this computer, without the transcription sentence (ADR 023)', async () => {
    await openSources(() => imported(makeSource()));

    expect(screen.getByText("Відео залишається на цьому комп'ютері.")).toBeVisible();
    // The sentence about the audio sent for transcription comes in M3, with the transcription.
    expect(screen.queryByText(/Для транскрипції/)).not.toBeInTheDocument();
    expect(screen.queryByText(/надсилається вибране аудіо/)).not.toBeInTheDocument();
  });

  it('shows a loading status and blocks a second import while the first is running', async () => {
    const { importVideo } = await openSources(() => new Promise<ImportVideoResult>(() => undefined));

    fireEvent.click(screen.getByTestId('import-video'));
    fireEvent.click(screen.getByTestId('import-video'));

    expect(await screen.findByTestId('import-loading')).toBeVisible();
    expect(screen.getByTestId('import-video')).toBeDisabled();
    expect(screen.getByTestId('import-video')).toHaveAccessibleDescription(
      /Додаємо відео/,
    );
    expect(importVideo).toHaveBeenCalledTimes(1);
  });

  it('reports a canceled picker without adding a source and allows another try', async () => {
    const { importVideo } = await openSources(() => ({ canceled: true }));

    fireEvent.click(screen.getByTestId('import-video'));

    expect(await screen.findByTestId('import-canceled')).toHaveTextContent(
      'Імпорт скасовано. Відео не додано.',
    );
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('import-loading')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('import-video')).toBeEnabled();

    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByTestId('import-canceled');
    expect(importVideo).toHaveBeenCalledTimes(2);
  });

  it('shows the Ukrainian error with details, and a retry that can succeed, for a code that is not about the file', async () => {
    let attempts = 0;
    const { importVideo } = await openSources(() => {
      attempts += 1;
      return attempts === 1
        ? failure('PROBE_TIMEOUT', 'Перевірка файлу перевищила ліміт часу; спробуйте ще раз', true)
        : imported(makeSource());
    });

    fireEvent.click(screen.getByTestId('import-video'));

    const alert = await screen.findByTestId('import-error');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(within(alert).getByRole('heading', { name: 'Не вдалося додати відео' })).toBeInTheDocument();
    expect(alert).not.toHaveTextContent('Не вдалося прочитати відео');
    expect(alert).toHaveTextContent('Перевірка файлу перевищила ліміт часу; спробуйте ще раз');
    expect(within(alert).getByText('Деталі')).toBeInTheDocument();
    expect(alert).toHaveTextContent('PROBE_TIMEOUT');
    expect(within(alert).getByTestId('import-retry')).toHaveTextContent('Спробувати ще раз');
    expect(screen.queryByRole('button', { name: 'Вибрати інший файл' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Спробувати ще раз' }));

    expect(await screen.findByTestId('source-ready')).toHaveTextContent('00:00:12.000');
    expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();
    expect(importVideo).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['SOURCE_UNREADABLE', 'Файл пошкоджений або має непідтримуваний формат'],
    ['SOURCE_NO_VIDEO', 'У файлі немає відеодоріжки'],
  ])(
    'says «Не вдалося прочитати відео» for %s, offers «Вибрати інший файл» and opens the picker again',
    async (code, message) => {
      let attempts = 0;
      const { importVideo } = await openSources(() => {
        attempts += 1;
        return attempts === 1 ? failure(code, message) : imported(makeSource());
      });

      fireEvent.click(screen.getByTestId('import-video'));

      const alert = await screen.findByTestId('import-error');
      expect(alert).toHaveAttribute('role', 'alert');
      expect(within(alert).getByRole('heading', { name: 'Не вдалося прочитати відео' })).toBeInTheDocument();
      expect(alert).not.toHaveTextContent('Не вдалося додати відео');
      expect(alert).toHaveTextContent(message);
      // «Деталі» keeps the code, as for every other error.
      expect(within(alert).getByText('Деталі')).toBeInTheDocument();
      expect(alert).toHaveTextContent(`Код помилки: ${code}`);
      const choose = within(alert).getByRole('button', { name: 'Вибрати інший файл' });
      expect(choose).toBe(screen.getByTestId('import-retry'));
      expect(screen.queryByRole('button', { name: 'Спробувати ще раз' })).not.toBeInTheDocument();
      expect(screen.queryByTestId('source-ready')).not.toBeInTheDocument();
      expect(importVideo).toHaveBeenCalledTimes(1);

      fireEvent.click(choose);

      // The same action as «Додати відео»: no file token, so main opens the picker again.
      expect(await screen.findByTestId('source-ready')).toHaveTextContent('00:00:12.000');
      expect(screen.queryByTestId('import-error')).not.toBeInTheDocument();
      expect(importVideo.mock.calls.map(([params]) => params)).toEqual([
        { project_id: PROJECT_ID },
        { project_id: PROJECT_ID },
      ]);
    },
  );

  it('turns a rejected bridge call into the same error state', async () => {
    await openSources(() => {
      throw new Error('IPC refused');
    });

    fireEvent.click(screen.getByTestId('import-video'));

    const alert = await screen.findByTestId('import-error');
    expect(alert).toHaveTextContent("Не вдалося зв'язатися з локальним рушієм");
    expect(screen.getByTestId('import-retry')).toBeEnabled();
  });
});

describe('Sources tab: sources of the project', () => {
  it('labels an unavailable or changed file and a failed hash with text, not only colour', async () => {
    await openSources(() =>
      imported(makeSource({ availability: 'changed', hash_status: 'failed' })),
    );

    fireEvent.click(screen.getByTestId('import-video'));

    await screen.findByTestId('source-ready');
    expect(screen.getByTestId('source-availability')).toHaveTextContent('⚠ Файл змінено після додавання');
    expect(screen.getByTestId('source-hash-status')).toHaveTextContent('✕ Не вдалося перевірити');
  });

  it('falls back to «Невідомо» for a status the UI does not know', async () => {
    await openSources(() =>
      imported(makeSource({ availability: 'offline', hash_status: 'constructor' })),
    );

    fireEvent.click(screen.getByTestId('import-video'));

    await screen.findByTestId('source-ready');
    expect(screen.getByTestId('source-availability')).toHaveTextContent('Невідомо');
    expect(screen.getByTestId('source-hash-status')).toHaveTextContent('Невідомо');
  });

  it('refreshes a pending hash status in the background until it is computed', async () => {
    let asked = 0;
    const { call } = await openSources(() => imported(makeSource({ hash_status: 'pending' })), {
      'sources.get': ({ id }) => {
        asked += 1;
        return ok(makeSource({ id, hash_status: 'computed' }));
      },
    });

    fireEvent.click(screen.getByTestId('import-video'));
    expect(await screen.findByTestId('source-hash-status')).toHaveTextContent('Перевіряється у фоні');

    expect(await screen.findByText('Підтверджена', {}, { timeout: 5000 })).toBeVisible();
    expect(call).toHaveBeenCalledWith('sources.get', { id: makeSource().id });
    const askedWhenDone = asked;
    await new Promise((resolve) => setTimeout(resolve, 2500));
    expect(asked).toBe(askedWhenDone);
  }, 15_000);

  it('keeps several sources selectable and shows the chosen one', async () => {
    const first = makeSource();
    const second = makeSource({
      id: '9b2e5a7d-3c4f-4d6e-9f80-1b0c2d3e4f5a',
      display_name: 'two_tracks.mkv',
      duration_ms: 3_723_004,
    });
    const sources = [first, second];
    await openSources(() => imported(sources.shift() ?? first));

    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByTestId('source-ready');
    expect(screen.queryByRole('list', { name: 'Джерела проєкту' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByRole('list', { name: 'Джерела проєкту' });
    expect(screen.getByTestId('source-ready')).toHaveTextContent('two_tracks.mkv');
    expect(screen.getByTestId('source-ready')).toHaveTextContent('01:02:03.004');
    expect(screen.getAllByTestId('source-ready')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'color_gop.mp4' }));

    expect(screen.getByTestId('source-ready')).toHaveTextContent('00:00:12.000');
    expect(screen.getByRole('button', { name: 'color_gop.mp4' })).toHaveAttribute(
      'aria-current',
      'true',
    );
  });

  it('does not list the same source twice when it is registered again', async () => {
    await openSources(() => imported(makeSource()));

    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByTestId('source-ready');
    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByTestId('source-ready');

    expect(screen.queryByRole('list', { name: 'Джерела проєкту' })).not.toBeInTheDocument();
    expect(screen.getAllByTestId('source-ready')).toHaveLength(1);
  });

  it('shows the sources of a project again when it is reopened, as the backend lists them', async () => {
    // The sources of a project come from the backend (ADR 023), not from this session's memory.
    const registered: SourceDto[] = [];
    await openSources(
      () => {
        const source = makeSource();
        registered.push(source);
        return imported(source);
      },
      { 'sources.list': () => ok({ items: [...registered], next_cursor: null }) },
    );
    fireEvent.click(screen.getByTestId('import-video'));
    await screen.findByTestId('source-ready');

    const nav = screen.getByRole('navigation', { name: 'Основна навігація' });
    fireEvent.click(within(nav).getByRole('button', { name: 'Проєкти' }));
    fireEvent.click(await screen.findByTestId('project-item'));

    expect(await screen.findByTestId('source-ready')).toHaveTextContent('color_gop.mp4');
  });
});
