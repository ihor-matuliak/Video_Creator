// @vitest-environment node
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ClipforgeBridge } from '../shared/bridge';

const electron = vi.hoisted(() => ({
  exposeInMainWorld: vi.fn<(name: string, value: unknown) => void>(),
  invoke: vi.fn<(channel: string, ...args: unknown[]) => Promise<unknown>>(),
  getPathForFile: vi.fn<(file: unknown) => string>(),
}));

vi.mock('electron', () => ({
  contextBridge: { exposeInMainWorld: electron.exposeInMainWorld },
  ipcRenderer: { invoke: electron.invoke },
  webUtils: { getPathForFile: electron.getPathForFile },
}));

const ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
const TOKEN = 'ab'.repeat(32);
const DROPPED = 'C:\\Users\\someone\\Videos\\clip.mp4';

let exposedName = '';
let bridge: ClipforgeBridge;

beforeAll(async () => {
  await import('./index');
  const [call] = electron.exposeInMainWorld.mock.calls;
  exposedName = call?.[0] ?? '';
  bridge = call?.[1] as ClipforgeBridge;
});

beforeEach(() => {
  electron.invoke.mockReset();
  electron.getPathForFile.mockReset();
});

function file(): File {
  return new File(['x'], 'clip.mp4', { type: 'video/mp4' });
}

describe('the exposed bridge', () => {
  it('is window.clipforge: frozen, version 2, exactly these methods', () => {
    expect(exposedName).toBe('clipforge');
    expect(Object.isFrozen(bridge)).toBe(true);
    expect(Object.keys(bridge).sort()).toEqual([
      'bridgeVersion',
      'call',
      'chooseExportFolder',
      'forgetApiKey',
      'getApiKeyStatus',
      'getStatus',
      'getViewState',
      'importTranscript',
      'importVideo',
      'openExportFolder',
      'pickVideo',
      'setApiKey',
      'setViewState',
      'takeDroppedVideo',
    ]);
    expect(bridge.bridgeVersion).toBe(2);
  });
});

const KEY = 'sk-or-v1-0123456789abcdefghij';

describe('the OpenRouter key (ADR 034)', () => {
  it('setApiKey sends the key on its own channel only, never on api:call, and answers the status', async () => {
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { state: 'protected', message: null } });

    const result = await bridge.setApiKey(KEY);

    expect(electron.invoke.mock.calls).toEqual([['settings:set-api-key', KEY]]);
    expect(result).toEqual({ ok: true, data: { state: 'protected', message: null } });
    expect(JSON.stringify(result)).not.toContain(KEY);
  });

  it('does not hand on the key, even one that main sent along by mistake', async () => {
    electron.invoke.mockResolvedValueOnce({
      ok: true,
      data: { state: 'session', message: null, key: KEY, openrouter_api_key: KEY },
      key: KEY,
    });
    electron.invoke.mockResolvedValueOnce({ state: 'none', message: null, key: KEY });

    const answers = [await bridge.setApiKey(KEY), await bridge.getApiKeyStatus()];

    expect(JSON.stringify(answers)).not.toContain(KEY);
    expect(answers).toEqual([
      { ok: true, data: { state: 'session', message: null } },
      { state: 'none', message: null },
    ]);
  });

  it('setApiKey passes an error through and refuses anything but a string before any IPC', async () => {
    electron.invoke.mockResolvedValueOnce({
      ok: false,
      error: { code: 'INVALID_API_KEY', message: 'Ключ має…', retryable: false },
    });

    expect(await bridge.setApiKey('short')).toEqual({
      ok: false,
      error: { code: 'INVALID_API_KEY', message: 'Ключ має…', retryable: false },
    });
    electron.invoke.mockClear();
    for (const value of [undefined, null, 5, { key: KEY }, [KEY]]) {
      await expect(bridge.setApiKey(value as unknown as string)).rejects.toThrow(TypeError);
    }
    expect(electron.invoke).not.toHaveBeenCalled();
  });

  it('forgetApiKey and getApiKeyStatus take no argument and answer the status', async () => {
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { state: 'none', message: null } });
    electron.invoke.mockResolvedValueOnce({ state: 'none', message: 'Не вдалося прочитати' });

    expect(await bridge.forgetApiKey()).toEqual({ ok: true, data: { state: 'none', message: null } });
    expect(await bridge.getApiKeyStatus()).toEqual({ state: 'none', message: 'Не вдалося прочитати' });
    expect(electron.invoke.mock.calls).toEqual([
      ['settings:forget-api-key'],
      ['settings:get-api-key-status'],
    ]);
  });

  it('rejects an answer of another shape and names the channel', async () => {
    for (const answer of [undefined, null, {}, { ok: true, data: { state: 'maybe', message: null } }]) {
      electron.invoke.mockResolvedValueOnce(answer);

      await expect(bridge.setApiKey(KEY)).rejects.toThrow('settings:set-api-key');
    }
    electron.invoke.mockResolvedValueOnce({ state: 'maybe', message: null });
    await expect(bridge.getApiKeyStatus()).rejects.toThrow('settings:get-api-key-status');
  });

  it('call sends settings.get and settings.update on api:call and reads the settings back', async () => {
    const settings = {
      stt_model: 'microsoft/mai-transcribe-2',
      stt_language: null,
      analysis_model: null,
      run_budget_usd: '2.50',
      has_api_key: true,
    };
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { ...settings, extra: 1 } });
    electron.invoke.mockResolvedValueOnce({ ok: true, data: settings });

    expect(await bridge.call('settings.get', {})).toEqual({ ok: true, data: settings });
    expect(await bridge.call('settings.update', { run_budget_usd: '2.50' })).toEqual({
      ok: true,
      data: settings,
    });
    expect(electron.invoke.mock.calls).toEqual([
      ['api:call', { op: 'settings.get', params: {} }],
      ['api:call', { op: 'settings.update', params: { run_budget_usd: '2.50' } }],
    ]);
  });
});

describe('transcription ops and jobs.resume (ADR 038)', () => {
  const estimate = {
    source_id: ID,
    audio_stream_index: 1,
    model: 'microsoft/mai-transcribe-2',
    language: null,
    duration_ms: 125_000,
    chunk_count: 1,
    billed_seconds: 125,
    usd_per_second_upper: '0.0001',
    estimate_micro_usd: 12_500,
    price_basis: 'upper_bound_per_second',
    budget_usd: null,
    within_budget: null,
    existing: { transcript_id: ID, run_id: ID },
  };
  const item = {
    id: ID,
    source_id: ID,
    job_id: ID,
    model: 'microsoft/mai-transcribe-2',
    status: 'completed',
    revision_id: ID,
    error_code: null,
    committed_micro_usd: 12_500,
    created_at: '2026-11-16T10:00:00Z',
  };
  const run = {
    id: ID,
    source_id: ID,
    job_id: ID,
    model: 'microsoft/mai-transcribe-2',
    status: 'running',
    revision_id: null,
    error_code: null,
    committed_micro_usd: 0,
    chunks: [
      { id: ID, core_start_ms: 0, core_end_ms: 60_000, state: 'running', attempts: 1, error_code: null },
    ],
  };
  const job = { id: ID, type: 'transcription', state: 'queued', progress: {}, error: null };

  it('call sends each op on api:call with its params and reads the answer back', async () => {
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { ...estimate, extra: 1 } });
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { id: ID, job_id: ID, status: 'queued' } });
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { ...run, extra: 1 } });
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { items: [item] } });
    electron.invoke.mockResolvedValueOnce({ ok: true, data: job });

    const results = [
      await bridge.call('transcription.estimate', { source_id: ID, audio_stream_index: 1 }),
      await bridge.call('transcription.create', { source_id: ID, audio_stream_index: 1, force: true }),
      await bridge.call('transcription.get', { id: ID }),
      await bridge.call('transcription.list', { source_id: ID }),
      await bridge.call('jobs.resume', { id: ID, retry_unknown: true }),
    ];

    expect(electron.invoke.mock.calls).toEqual([
      ['api:call', { op: 'transcription.estimate', params: { source_id: ID, audio_stream_index: 1 } }],
      [
        'api:call',
        { op: 'transcription.create', params: { source_id: ID, audio_stream_index: 1, force: true } },
      ],
      ['api:call', { op: 'transcription.get', params: { id: ID } }],
      ['api:call', { op: 'transcription.list', params: { source_id: ID } }],
      ['api:call', { op: 'jobs.resume', params: { id: ID, retry_unknown: true } }],
    ]);
    expect(results).toEqual([
      { ok: true, data: estimate },
      { ok: true, data: { id: ID, job_id: ID, status: 'queued' } },
      { ok: true, data: run },
      { ok: true, data: { items: [item] } },
      { ok: true, data: job },
    ]);
  });

  it('call rejects an answer of another shape for each op', async () => {
    const cases = [
      ['transcription.estimate', { source_id: ID, audio_stream_index: 1 }, { ...estimate, within_budget: 'yes' }],
      ['transcription.create', { source_id: ID, audio_stream_index: 1 }, { id: ID, job_id: 5, status: 'queued' }],
      ['transcription.get', { id: ID }, { ...run, chunks: 'none' }],
      ['transcription.list', { source_id: ID }, { items: [{ ...item, committed_micro_usd: '1' }] }],
      ['jobs.resume', { id: ID }, { ...job, error: { code: 'X' } }],
    ] as const;

    for (const [op, params, data] of cases) {
      electron.invoke.mockResolvedValueOnce({ ok: true, data });

      await expect(bridge.call(op, params as never), op).rejects.toThrow(TypeError);
    }
  });
});

describe('pickVideo', () => {
  it('asks main on dialog:pick-video with no argument and returns the token and the file name', async () => {
    electron.invoke.mockResolvedValueOnce({ token: TOKEN, fileName: 'clip.mp4' });

    const result = await bridge.pickVideo();

    expect(electron.invoke.mock.calls).toEqual([['dialog:pick-video']]);
    expect(result).toEqual({ token: TOKEN, fileName: 'clip.mp4' });
  });

  it('passes a cancelled dialog and an error through', async () => {
    electron.invoke.mockResolvedValueOnce({ canceled: true });
    electron.invoke.mockResolvedValueOnce({
      ok: false,
      error: { code: 'BRIDGE_ERROR', message: 'Не вдалося', retryable: true },
    });

    expect(await bridge.pickVideo()).toEqual({ canceled: true });
    expect(await bridge.pickVideo()).toEqual({
      ok: false,
      error: { code: 'BRIDGE_ERROR', message: 'Не вдалося', retryable: true },
    });
  });

  it('never hands on a path, even one that main sent along', async () => {
    electron.invoke.mockResolvedValueOnce({ token: TOKEN, fileName: 'clip.mp4', path: DROPPED });

    const result = await bridge.pickVideo();

    expect(result).toEqual({ token: TOKEN, fileName: 'clip.mp4' });
    expect(JSON.stringify(result)).not.toContain('Users');
  });

  it('rejects an answer of another shape, a path in place of the token included', async () => {
    for (const answer of [
      undefined,
      null,
      {},
      { token: DROPPED, fileName: 'clip.mp4' },
      { token: TOKEN },
      { ok: true, data: { token: TOKEN, fileName: 'clip.mp4' } },
    ]) {
      electron.invoke.mockResolvedValueOnce(answer);

      await expect(bridge.pickVideo()).rejects.toThrow(TypeError);
    }
  });
});

describe('takeDroppedVideo', () => {
  it('reads the path of the file here, sends only { path } to drop:video and returns the choice', async () => {
    electron.getPathForFile.mockReturnValueOnce(DROPPED);
    electron.invoke.mockResolvedValueOnce({ token: TOKEN, fileName: 'clip.mp4' });
    const dropped = file();

    const result = await bridge.takeDroppedVideo(dropped);

    expect(electron.getPathForFile).toHaveBeenCalledTimes(1);
    expect(electron.getPathForFile).toHaveBeenCalledWith(dropped);
    expect(electron.invoke.mock.calls).toEqual([['drop:video', { path: DROPPED }]]);
    expect(result).toEqual({ token: TOKEN, fileName: 'clip.mp4' });
  });

  it('does not return the path: not in the answer, not in an error, not in a hostile reply', async () => {
    electron.getPathForFile.mockReturnValue(DROPPED);
    electron.invoke.mockResolvedValueOnce({ token: TOKEN, fileName: 'clip.mp4', path: DROPPED });
    electron.invoke.mockResolvedValueOnce({
      ok: false,
      error: { code: 'FILE_NOT_FOUND', message: 'Файл не знайдено', retryable: false },
      path: DROPPED,
    });

    const answers = [await bridge.takeDroppedVideo(file()), await bridge.takeDroppedVideo(file())];

    expect(JSON.stringify(answers)).not.toContain('Users');
    expect(JSON.stringify(answers)).not.toContain('someone');
    expect(answers[1]).toEqual({
      ok: false,
      error: { code: 'FILE_NOT_FOUND', message: 'Файл не знайдено', retryable: false },
    });
  });

  it('answers UNSUPPORTED_FILE without any IPC when the file has no path on the disk', async () => {
    electron.getPathForFile.mockReturnValueOnce('');

    const result = await bridge.takeDroppedVideo(file());

    expect(result).toEqual({
      ok: false,
      error: {
        code: 'UNSUPPORTED_FILE',
        message: 'Це не відеофайл. Перетягніть файл MP4, MOV, MKV…',
        retryable: false,
      },
    });
    expect(electron.invoke).not.toHaveBeenCalled();
  });

  it('accepts only a File: anything else is refused before the path is read or main is asked', async () => {
    const lookalike = { name: 'clip.mp4', path: DROPPED, size: 1, type: 'video/mp4' };

    for (const value of [
      DROPPED,
      lookalike,
      { path: DROPPED },
      new Blob(['x']),
      null,
      undefined,
      5,
      [file()],
    ]) {
      await expect(bridge.takeDroppedVideo(value as unknown as File)).rejects.toThrow(TypeError);
    }
    expect(electron.getPathForFile).not.toHaveBeenCalled();
    expect(electron.invoke).not.toHaveBeenCalled();
  });

  it('rejects an answer of another shape and names the channel', async () => {
    electron.getPathForFile.mockReturnValue(DROPPED);

    for (const answer of [undefined, null, {}, { token: 'abc', fileName: 'clip.mp4' }, { ok: true }]) {
      electron.invoke.mockResolvedValueOnce(answer);

      await expect(bridge.takeDroppedVideo(file())).rejects.toThrow('drop:video');
    }
  });
});

describe('importVideo and system.checks', () => {
  it('importVideo sends the project and the token as they are and reads a source back', async () => {
    const source = {
      id: ID,
      project_id: ID,
      display_name: 'clip.mp4',
      size: 10,
      duration_ms: 12000,
      video_stream_index: 0,
      audio_streams: [],
      availability: 'available',
      hash_status: 'pending',
      hash_job_id: null,
      created_at: '2026-11-15T10:00:00Z',
      playback: { direct: true, reason: null, direct_audio_stream_index: null },
      video: { width: 320, height: 180, frame_rate: '30/1', vfr: false },
    };
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { ...source, path: DROPPED } });

    const result = await bridge.importVideo({ project_id: ID, file_token: TOKEN });

    expect(electron.invoke.mock.calls).toEqual([
      ['dialog:import-video', { project_id: ID, file_token: TOKEN }],
    ]);
    expect(result).toEqual({ ok: true, data: source });
  });

  it('call sends system.checks on api:call and keeps only the fields of the answer', async () => {
    electron.invoke.mockResolvedValueOnce({
      ok: true,
      data: {
        ffmpeg: { ok: true, version: '8.0.1', path: 'C:\\ffmpeg.exe' },
        ffprobe: { ok: true, version: '8.0.1' },
        data_dir: { ok: true, free_bytes: 42 },
      },
    });

    const result = await bridge.call('system.checks', {});

    expect(electron.invoke.mock.calls).toEqual([['api:call', { op: 'system.checks', params: {} }]]);
    expect(result).toEqual({
      ok: true,
      data: {
        ffmpeg: { ok: true, version: '8.0.1' },
        ffprobe: { ok: true, version: '8.0.1' },
        data_dir: { ok: true, free_bytes: 42 },
      },
    });
  });

  it('call rejects a system.checks answer of another shape', async () => {
    electron.invoke.mockResolvedValueOnce({ ok: true, data: { ffmpeg: { ok: true, version: null } } });

    await expect(bridge.call('system.checks', {})).rejects.toThrow(TypeError);
  });
});

describe('importTranscript (ADR 039)', () => {
  const REVISION = {
    id: ID,
    source_id: ID,
    revision: 1,
    origin: 'import',
    run_id: null,
    parent_id: null,
    timing_level: 'segment',
    language: 'uk',
    duration_ms: 12000,
    word_count: 0,
    segment_count: 1,
    coverage: [{ start_ms: 0, end_ms: 12000, state: 'succeeded' }],
    created_at: '2026-11-16T10:00:00Z',
  };

  it('sends the params as they are on dialog:import-transcript and reads the revision back', async () => {
    electron.invoke.mockResolvedValueOnce({
      ok: true,
      data: { ...REVISION, metadata: { provider: 'import' } },
    });

    const result = await bridge.importTranscript({ source_id: ID, offset_ms: 1500 });

    expect(electron.invoke.mock.calls).toEqual([
      ['dialog:import-transcript', { source_id: ID, offset_ms: 1500 }],
    ]);
    expect(result).toEqual({ ok: true, data: REVISION });
  });

  it('answers a cancel as a cancel and an error as an error', async () => {
    electron.invoke.mockResolvedValueOnce({ canceled: true });
    expect(await bridge.importTranscript({ source_id: ID })).toEqual({ canceled: true });

    const failure = {
      ok: false,
      error: {
        code: 'TRANSCRIPT_FILE_TOO_LARGE',
        message: 'Файл субтитрів більший за 20 МБ',
        retryable: false,
      },
    };
    electron.invoke.mockResolvedValueOnce(failure);
    expect(await bridge.importTranscript({ source_id: ID })).toEqual(failure);
  });

  it('never hands on a path or the text of the file, even when main sent them along', async () => {
    electron.invoke.mockResolvedValueOnce({
      ok: true,
      data: { ...REVISION, path: DROPPED, text: 'Таємний текст' },
    });

    const result = await bridge.importTranscript({ source_id: ID });

    expect(result).toEqual({ ok: true, data: REVISION });
    const serialized = JSON.stringify(result);
    expect(serialized).not.toContain('clip.mp4');
    expect(serialized).not.toContain('Таємний');
    expect(serialized).not.toContain('Users');
  });

  it('keeps a cancel exact and drops any other key of it', async () => {
    electron.invoke.mockResolvedValueOnce({ canceled: true, path: DROPPED });

    expect(await bridge.importTranscript({ source_id: ID })).toEqual({ canceled: true });
  });

  it('rejects an answer of another shape and names the channel', async () => {
    electron.invoke.mockResolvedValueOnce({ canceled: 'yes' });
    await expect(bridge.importTranscript({ source_id: ID })).rejects.toThrow(
      'dialog:import-transcript',
    );

    electron.invoke.mockResolvedValueOnce({ ok: true, data: { id: ID } });
    await expect(bridge.importTranscript({ source_id: ID })).rejects.toThrow(TypeError);
  });
});
