import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PLAYBACK_REASONS } from '../shared/api';
import type { SourceDto, TranscriptDto, TranscriptionEstimateDto } from '../shared/api';
import { apiFailure } from '../shared/bridge';
import type { ImportTranscriptResult } from '../shared/bridge';
import { SourceInspector } from './SourceInspector';
import { NO_KEY, SOURCE_ID, deferred, flushAnswers, installBridge, makeSettings, makeSource, ok } from './test-bridge';

function show(source: SourceDto): void {
  render(<SourceInspector source={source} />);
}

const PROXY_BASE = { direct: false, direct_audio_stream_index: null } as const;

describe('SourceInspector: video facts (docs/03 section 5)', () => {
  it('shows the resolution, the frame rate, the constant-rate badge and the viewing way', () => {
    show(
      makeSource({
        video: { width: 1920, height: 1080, frame_rate: '30000/1001', vfr: false },
        playback: { direct: true, reason: null, direct_audio_stream_index: 1 },
      }),
    );

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^1920×1080$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^29,97 к\/с$/);
    expect(screen.getByTestId('source-vfr')).toHaveTextContent('✓ Постійна частота кадрів');
    expect(screen.getByTestId('source-playback')).toHaveTextContent(/^Напряму$/);
  });

  it('shows the whole number of frames without decimals', () => {
    show(makeSource({ video: { width: 320, height: 180, frame_rate: '30/1', vfr: false } }));

    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^30 к\/с$/);
  });

  it('shows a VFR source with a warning badge: an icon and the text', () => {
    show(makeSource({ video: { width: 320, height: 180, frame_rate: '30/1', vfr: true } }));

    const badge = screen.getByTestId('source-vfr');
    expect(badge).toHaveTextContent('⚠ Змінна частота кадрів (VFR)');
    expect(badge).toHaveClass('status-warning');
    expect(badge.querySelector('[aria-hidden="true"]')).toHaveTextContent('⚠');
  });

  it('says it is not known when the probe did not give a fact', () => {
    show(makeSource({ video: { width: null, height: null, frame_rate: null, vfr: null } }));

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-vfr')).toHaveTextContent(/^◌ VFR: невідомо$/);
    // The frame rate and the badge side by side read as two different unknowns.
    expect(screen.getByTestId('source-fps').parentElement).toHaveTextContent(
      /^невідомо ◌ VFR: невідомо$/,
    );
  });

  it('does not make up a resolution from half of it', () => {
    show(makeSource({ video: { width: 1920, height: null, frame_rate: '25/1', vfr: false } }));

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^25 к\/с$/);
  });

  it('says that the video stays on this computer and that the chosen audio is sent for transcription', () => {
    show(makeSource());

    const note = "Відео залишається на цьому комп'ютері. Для транскрипції надсилається вибране аудіо.";
    expect(screen.getByTestId('source-local-note')).toHaveTextContent(note);
    expect(screen.getByTestId('source-local-note').textContent).toBe(note);
  });
});

describe('SourceInspector: how the source is viewed', () => {
  it.each([
    [PLAYBACK_REASONS.container, 'контейнер файлу не підтримується програвачем'],
    [PLAYBACK_REASONS.videoCodec, 'відеокодек не підтримується програвачем'],
    [PLAYBACK_REASONS.audioCodec, 'звуковий кодек першої доріжки не підтримується програвачем'],
    [PLAYBACK_REASONS.origin, 'відео починається не з нульової позначки часу'],
    [PLAYBACK_REASONS.missing, 'не вдалося визначити, чи підтримується оригінал програвачем'],
  ])('needs a copy for %s, and says why in Ukrainian', (reason, why) => {
    show(makeSource({ playback: { ...PROXY_BASE, reason } }));

    const playback = screen.getByTestId('source-playback');
    expect(playback).toHaveTextContent(`Потрібна копія для перегляду. Причина: ${why}`);
    expect(playback).not.toHaveTextContent(reason);
    expect(playback).not.toHaveTextContent('Напряму');
  });

  it('says "Невідомо" for a reason code it does not know', () => {
    show(makeSource({ playback: { ...PROXY_BASE, reason: 'SOMETHING_NEW' } }));

    const playback = screen.getByTestId('source-playback');
    expect(playback).toHaveTextContent('Потрібна копія для перегляду. Причина: Невідомо');
    expect(playback).not.toHaveTextContent('SOMETHING_NEW');
  });
});

describe('SourceInspector: the recognition block gets the state of the file (ADR 038, доповнення 3)', () => {
  const ESTIMATE: TranscriptionEstimateDto = {
    source_id: 'source',
    audio_stream_index: 1,
    model: 'microsoft/mai-transcribe-2',
    language: null,
    duration_ms: 12_000,
    chunk_count: 1,
    billed_seconds: 12,
    usd_per_second_upper: '0.0001',
    estimate_micro_usd: 1200,
    price_basis: 'live_probe_2026-11-15_x3',
    budget_usd: '2.50',
    within_budget: true,
    existing: null,
  };

  function installWithCreate(created: unknown[]): void {
    installBridge({
      handlers: {
        'settings.get': () => ok(makeSettings({ has_api_key: true })),
        'transcription.estimate': () => ok(ESTIMATE),
        'transcription.list': () => ok({ items: [] }),
        'transcription.create': (params) => {
          created.push(params);
          return ok({ id: 'run', job_id: 'job', status: 'queued' });
        },
      },
    });
  }

  it('keeps the start off for a file that is missing, says why, and sends nothing', async () => {
    const created: unknown[] = [];
    installWithCreate(created);
    render(<SourceInspector source={makeSource({ availability: 'missing' })} />);

    const start = await screen.findByRole('button', { name: 'Розпізнати мовлення' });
    expect(start).toBeDisabled();
    expect(start).toHaveAccessibleDescription('Файл не знайдено на цьому місці: розпізнавання неможливе.');
    fireEvent.click(start);
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    expect(created).toEqual([]);
  });

  it('keeps the start on for an available file', async () => {
    installWithCreate([]);
    render(<SourceInspector source={makeSource()} />);

    expect(await screen.findByRole('button', { name: 'Розпізнати мовлення' })).toBeEnabled();
  });
});

describe('SourceInspector: importing subtitles (ADR 039)', () => {
  const IMPORT = 'Імпортувати субтитри (SRT, VTT)';
  const OTHER_SOURCE_ID = '0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e';
  const REVISION: TranscriptDto = {
    id: '6e8a0c2b-4d6f-4a8b-9c1d-2e3f4a5b6c7d',
    source_id: SOURCE_ID,
    revision: 2,
    origin: 'import',
    run_id: null,
    parent_id: null,
    timing_level: 'segment',
    language: 'en',
    duration_ms: 12_000,
    word_count: 0,
    segment_count: 5,
    coverage: [],
    created_at: '2026-11-16T10:00:00Z',
  };

  it('imports the chosen file as a new revision and says its number and its segments', async () => {
    const installed = installBridge({ importTranscript: () => ok(REVISION) });
    render(<SourceInspector source={makeSource()} />);

    fireEvent.click(screen.getByRole('button', { name: IMPORT }));

    const result = await screen.findByTestId('transcript-import-result');
    expect(result).toHaveAttribute('role', 'status');
    expect(result).toHaveTextContent('Субтитри імпортовано: ревізія 2, сегментів 5.');
    expect(installed.importTranscript).toHaveBeenCalledWith({ source_id: SOURCE_ID });
  });

  it('needs no key, and is offered for a file that is missing', async () => {
    const installed = installBridge({ apiKey: NO_KEY, importTranscript: () => ok(REVISION) });
    render(<SourceInspector source={makeSource({ availability: 'missing' })} />);

    const button = screen.getByRole('button', { name: IMPORT });
    expect(button).toBeEnabled();
    fireEvent.click(button);

    expect(await screen.findByTestId('transcript-import-result')).toHaveTextContent(
      'Субтитри імпортовано: ревізія 2, сегментів 5.',
    );
    // The bridge holds no key, and the import went through all the same.
    expect(installed.importTranscript).toHaveBeenCalledWith({ source_id: SOURCE_ID });
  });

  it('changes nothing when the dialog is cancelled', async () => {
    const reply = deferred<ImportTranscriptResult>();
    installBridge({ importTranscript: () => reply.promise });
    render(<SourceInspector source={makeSource()} />);

    const button = screen.getByRole('button', { name: IMPORT });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    await act(async () => {
      reply.resolve({ canceled: true });
      await flushAnswers();
    });

    expect(button).toBeEnabled();
    expect(screen.queryByTestId('transcript-import-result')).toBeNull();
    expect(screen.queryByTestId('transcript-import-error')).toBeNull();
  });

  it('clears the result of an earlier import as soon as a new one starts, and it stays cleared when that dialog is cancelled', async () => {
    const replies = [deferred<ImportTranscriptResult>(), deferred<ImportTranscriptResult>()];
    let calls = 0;
    installBridge({
      importTranscript: () => {
        const reply = replies[calls];
        calls += 1;
        return reply === undefined ? { canceled: true } : reply.promise;
      },
    });
    render(<SourceInspector source={makeSource()} />);
    const button = screen.getByRole('button', { name: IMPORT });

    fireEvent.click(button);
    await act(async () => {
      replies[0]?.resolve(ok(REVISION));
      await flushAnswers();
    });
    expect(screen.getByTestId('transcript-import-result')).toHaveTextContent(
      'Субтитри імпортовано: ревізія 2, сегментів 5.',
    );

    // The line about the earlier import does not show while the new import is on its way.
    fireEvent.click(button);
    expect(screen.queryByTestId('transcript-import-result')).toBeNull();
    expect(screen.queryByTestId('transcript-import-error')).toBeNull();

    await act(async () => {
      replies[1]?.resolve({ canceled: true });
      await flushAnswers();
    });
    expect(screen.queryByTestId('transcript-import-result')).toBeNull();
    expect(screen.queryByTestId('transcript-import-error')).toBeNull();
    expect(button).toBeEnabled();
  });

  it('clears the failure of an earlier import when a new one starts', async () => {
    const replies = [deferred<ImportTranscriptResult>(), deferred<ImportTranscriptResult>()];
    let calls = 0;
    installBridge({
      importTranscript: () => {
        const reply = replies[calls];
        calls += 1;
        return reply === undefined ? { canceled: true } : reply.promise;
      },
    });
    render(<SourceInspector source={makeSource()} />);
    const button = screen.getByRole('button', { name: IMPORT });

    fireEvent.click(button);
    await act(async () => {
      replies[0]?.resolve(apiFailure('TRANSCRIPT_FILE_UNSUPPORTED', 'Оберіть файл субтитрів у форматі SRT або VTT'));
      await flushAnswers();
    });
    expect(screen.getByTestId('transcript-import-error')).toHaveTextContent('Оберіть файл субтитрів');

    fireEvent.click(button);
    expect(screen.queryByTestId('transcript-import-error')).toBeNull();

    await act(async () => {
      replies[1]?.resolve(ok(REVISION));
      await flushAnswers();
    });
    expect(screen.getByTestId('transcript-import-result')).toHaveTextContent(
      'Субтитри імпортовано: ревізія 2, сегментів 5.',
    );
    expect(screen.queryByTestId('transcript-import-error')).toBeNull();
  });

  it('shows the message of a failure with role alert', async () => {
    installBridge({
      importTranscript: () => apiFailure('TRANSCRIPT_FILE_UNSUPPORTED', 'Оберіть файл субтитрів у форматі SRT або VTT'),
    });
    render(<SourceInspector source={makeSource()} />);

    fireEvent.click(screen.getByRole('button', { name: IMPORT }));

    const error = await screen.findByTestId('transcript-import-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent('Оберіть файл субтитрів у форматі SRT або VTT');
    expect(screen.queryByTestId('transcript-import-result')).toBeNull();
  });

  it('turns a refused bridge call into a failure of the same kind, not an unhandled error', async () => {
    installBridge({
      importTranscript: () => {
        throw new Error('IPC refused');
      },
    });
    render(<SourceInspector source={makeSource()} />);

    fireEvent.click(screen.getByRole('button', { name: IMPORT }));

    const error = await screen.findByTestId('transcript-import-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent("Не вдалося зв'язатися з локальним рушієм. Спробуйте ще раз");
    expect(error).not.toHaveTextContent('IPC refused');
  });

  it('is disabled while the import is on its way, so a second click sends nothing', async () => {
    const reply = deferred<ImportTranscriptResult>();
    const installed = installBridge({ importTranscript: () => reply.promise });
    render(<SourceInspector source={makeSource()} />);

    const button = screen.getByRole('button', { name: IMPORT });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(installed.importTranscript).toHaveBeenCalledTimes(1);

    await act(async () => {
      reply.resolve({ canceled: true });
      await flushAnswers();
    });
    expect(button).toBeEnabled();
  });

  it('forgets its result when another source is shown', async () => {
    installBridge({ importTranscript: () => ok(REVISION) });
    const { rerender } = render(<SourceInspector source={makeSource()} />);

    fireEvent.click(screen.getByRole('button', { name: IMPORT }));
    await screen.findByTestId('transcript-import-result');

    rerender(<SourceInspector source={makeSource({ id: OTHER_SOURCE_ID })} />);
    expect(screen.queryByTestId('transcript-import-result')).toBeNull();
    expect(screen.getByRole('button', { name: IMPORT })).toBeEnabled();
  });
});
