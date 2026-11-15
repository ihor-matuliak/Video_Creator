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
      'getStatus',
      'getViewState',
      'importVideo',
      'openExportFolder',
      'pickVideo',
      'setViewState',
      'takeDroppedVideo',
    ]);
    expect(bridge.bridgeVersion).toBe(2);
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
