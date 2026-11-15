import { act } from '@testing-library/react';
import { vi } from 'vitest';
import type { Mock } from 'vitest';

import type {
  ApiData,
  ApiOp,
  ApiParams,
  ClipDto,
  ProjectDto,
  ProjectListItemDto,
  SourceDto,
  SystemChecksDto,
} from '../shared/api';
import type {
  ApiResult,
  AppStatus,
  ChooseFolderResult,
  ClipforgeBridge,
  FolderOpened,
  ImportVideoParams,
  ImportVideoResult,
  OpenFolderParams,
  PickVideoResult,
  SaveViewStateResult,
  ViewState,
} from '../shared/bridge';
import { WIDE_QUERY } from './layout-logic';

export const READY: AppStatus = { state: 'ready', mode: 'production', version: '0.1.0' };

export const PROJECT_ID = '3f2b8c1e-5d4a-4b7e-9a10-2c6d8e0f1a2b';
export const SOURCE_ID = '8a1d4f6c-2b3e-4c5d-8e7f-0a9b1c2d3e4f';
export const CLIP_ID = '5c7e9a1b-3d4f-4a6b-8c8d-9e0f1a2b3c4d';

export function makeProject(overrides: Partial<ProjectDto> = {}): ProjectDto {
  return {
    id: PROJECT_ID,
    name: 'Перевірка інтерфейсу',
    created_at: '2026-11-14T10:00:00Z',
    updated_at: '2026-11-14T10:00:00Z',
    archived_at: null,
    ...overrides,
  };
}

/** A project as the list answers it: no sources or clips yet, last active when it was updated. */
export function makeProjectItem(overrides: Partial<ProjectListItemDto> = {}): ProjectListItemDto {
  const project = makeProject(overrides);
  return {
    source_count: 0,
    clip_count: 0,
    last_activity_at: project.updated_at,
    ...project,
    ...overrides,
  };
}

export function makeSource(overrides: Partial<SourceDto> = {}): SourceDto {
  return {
    id: SOURCE_ID,
    project_id: PROJECT_ID,
    display_name: 'color_gop.mp4',
    size: 1024,
    duration_ms: 12_000,
    video_stream_index: 0,
    audio_streams: [
      { index: 1, codec: 'aac', channels: 2, language: 'uk', title: null, default: true, start_offset_ms: 0 },
    ],
    availability: 'available',
    hash_status: 'computed',
    hash_job_id: null,
    created_at: '2026-11-14T10:00:00Z',
    // Plays directly, like the real fixture; its first audio stream is index 1.
    playback: { direct: true, reason: null, direct_audio_stream_index: 1 },
    // A constant 30 fps picture, like the real fixture.
    video: { width: 320, height: 180, frame_rate: '30/1', vfr: false },
    ...overrides,
  };
}

export function makeClip(overrides: Partial<ClipDto> = {}): ClipDto {
  return {
    id: CLIP_ID,
    source_id: SOURCE_ID,
    analysis_run_id: null,
    revision: 1,
    title: 'Мій фрагмент',
    start_ms: 4120,
    end_ms: 7880,
    recommended_start_ms: null,
    recommended_end_ms: null,
    categories: [],
    evidence_type: 'manual',
    evidence_ids: [],
    reason: null,
    scores: null,
    rank: null,
    visual_review_required: false,
    review_status: 'suggested',
    export_state: 'none',
    created_at: '2026-11-14T10:05:00Z',
    updated_at: '2026-11-14T10:05:00Z',
    ...overrides,
  };
}

export function ok<T>(data: T): ApiResult<T> {
  return { ok: true, data };
}

export function failure(code: string, message: string, retryable = false): ApiResult<never> {
  return { ok: false, error: { code, message, retryable } };
}

export type ApiHandlers = {
  readonly [O in ApiOp]?: (params: ApiParams[O]) => ApiResult<ApiData[O]> | Promise<ApiResult<ApiData[O]>>;
};

type RecordedCall = (op: ApiOp, params: unknown) => Promise<ApiResult<unknown>>;
type AnyHandler = (params: never) => ApiResult<unknown> | Promise<ApiResult<unknown>>;

export interface InstalledBridge {
  readonly bridge: ClipforgeBridge;
  /** Every `bridge.call`, with the op and params as the renderer passed them. */
  readonly call: Mock<RecordedCall>;
  readonly importVideo: Mock<ClipforgeBridge['importVideo']>;
  readonly pickVideo: Mock<ClipforgeBridge['pickVideo']>;
  readonly takeDroppedVideo: Mock<ClipforgeBridge['takeDroppedVideo']>;
  readonly chooseExportFolder: Mock<ClipforgeBridge['chooseExportFolder']>;
  readonly openExportFolder: Mock<ClipforgeBridge['openExportFolder']>;
  readonly getViewState: Mock<ClipforgeBridge['getViewState']>;
  readonly setViewState: Mock<ClipforgeBridge['setViewState']>;
}

export interface BridgeOptions {
  readonly getStatus?: ClipforgeBridge['getStatus'];
  /**
   * Ops without a handler reject, like an IPC call that main refused. The exception is
   * `sources.list`, which answers an empty project unless a test gives it a handler.
   */
  readonly handlers?: ApiHandlers;
  /** The state file as the previous run left it; without it nothing is remembered. */
  readonly viewState?: ViewState | null;
  /** Replaces the read of the remembered state (to reject, or to answer late). */
  readonly getViewState?: () => ViewState | null | Promise<ViewState | null>;
  readonly importVideo?: (params: ImportVideoParams) => ImportVideoResult | Promise<ImportVideoResult>;
  /** The native video picker; without a handler the user cancels it. */
  readonly pickVideo?: () => PickVideoResult | Promise<PickVideoResult>;
  /** Without a handler the call rejects, like an IPC call that main refused. */
  readonly takeDroppedVideo?: (file: File) => PickVideoResult | Promise<PickVideoResult>;
  /** The native folder picker; without a handler the user cancels it. */
  readonly chooseExportFolder?: () => ChooseFolderResult | Promise<ChooseFolderResult>;
  /** Without a handler the call rejects, like an IPC call that main refused. */
  readonly openExportFolder?: (
    params: OpenFolderParams,
  ) => ApiResult<FolderOpened> | Promise<ApiResult<FolderOpened>>;
}

/** A well-formed one-time folder token, for tests. */
export const FOLDER_TOKEN = 'ab'.repeat(32);

/** A well-formed one-time video token (`pickVideo`, ADR 024), for tests. */
export const VIDEO_TOKEN = 'cd'.repeat(32);

/** The answer of `system.checks` on a machine that is ready: both tools found, the data folder writable. */
export function makeChecks(overrides: Partial<SystemChecksDto> = {}): SystemChecksDto {
  return {
    ffmpeg: { ok: true, version: '7.1' },
    ffprobe: { ok: true, version: '7.1' },
    data_dir: { ok: true, free_bytes: 12 * 1024 ** 3 },
    ...overrides,
  };
}

/** Puts a fake bridge on `window.clipforge`; the setup file removes it after each test. */
export function installBridge(options: BridgeOptions = {}): InstalledBridge {
  const handlers: ApiHandlers = {
    'sources.list': () => ok({ items: [], next_cursor: null }),
    ...options.handlers,
  };
  const call = vi.fn<RecordedCall>(async (op, params) => {
    const handler: AnyHandler | undefined = handlers[op];
    if (handler === undefined) {
      throw new Error(`Тестовий bridge: немає обробника для ${op}`);
    }
    return await handler(params as never);
  });
  const importVideo = vi.fn(async (params: ImportVideoParams): Promise<ImportVideoResult> => {
    if (options.importVideo === undefined) {
      throw new Error('Тестовий bridge: немає обробника importVideo');
    }
    return await options.importVideo(params);
  });
  const pickVideo = vi.fn(
    async (): Promise<PickVideoResult> =>
      options.pickVideo === undefined ? { canceled: true } : await options.pickVideo(),
  );
  const takeDroppedVideo = vi.fn(async (file: File): Promise<PickVideoResult> => {
    if (options.takeDroppedVideo === undefined) {
      throw new Error('Тестовий bridge: немає обробника takeDroppedVideo');
    }
    return await options.takeDroppedVideo(file);
  });
  const chooseExportFolder = vi.fn(
    async (): Promise<ChooseFolderResult> =>
      options.chooseExportFolder === undefined ? { canceled: true } : await options.chooseExportFolder(),
  );
  const openExportFolder = vi.fn(async (params: OpenFolderParams): Promise<ApiResult<FolderOpened>> => {
    if (options.openExportFolder === undefined) {
      throw new Error('Тестовий bridge: немає обробника openExportFolder');
    }
    return await options.openExportFolder(params);
  });
  // Like the state file: what was last saved is what the next read returns.
  let remembered: ViewState | null = options.viewState ?? null;
  const getViewState = vi.fn(
    async (): Promise<ViewState | null> =>
      options.getViewState === undefined ? remembered : await options.getViewState(),
  );
  const setViewState = vi.fn((state: ViewState | null): Promise<SaveViewStateResult> => {
    remembered = state;
    return Promise.resolve(ok({ saved: true as const }));
  });
  const bridge: ClipforgeBridge = {
    bridgeVersion: 2,
    getStatus: options.getStatus ?? (() => Promise.resolve(READY)),
    // The fake answers whatever the handler returns; the real bridge validates per op.
    call: ((op, params) => call(op, params)) as ClipforgeBridge['call'],
    importVideo,
    pickVideo,
    takeDroppedVideo,
    chooseExportFolder,
    openExportFolder,
    getViewState,
    setViewState,
  };
  Object.defineProperty(window, 'clipforge', { value: bridge, configurable: true });
  return {
    bridge,
    call,
    importVideo,
    pickVideo,
    takeDroppedVideo,
    chooseExportFolder,
    openExportFolder,
    getViewState,
    setViewState,
  };
}

/**
 * `projects.list` answering one page with the given projects. A plain project gets the figures
 * of an empty one (no sources, no clips); a list item is answered as it is.
 */
export function listOf(...items: (ProjectDto | ProjectListItemDto)[]): ApiHandlers['projects.list'] {
  return () => ok({ items: items.map((item) => makeProjectItem(item)), next_cursor: null });
}

export interface MediaControl {
  /** Moves the window across 1440 px: tells everything that listens to the media query. */
  readonly setWide: (wide: boolean) => void;
}

/**
 * jsdom has no media queries. Stands in for `window.matchMedia` and answers the 1440 px query
 * (docs/03 section 8) with `wide`; the setup file removes it after each test.
 */
export function installMediaQuery(wide: boolean): MediaControl {
  let current = wide;
  const listeners = new Set<() => void>();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => ({
      get matches() {
        return query === WIDE_QUERY && current;
      },
      media: query,
      addEventListener: (_type: string, listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: () => void) => {
        listeners.delete(listener);
      },
    }),
  });
  return {
    setWide: (next) => {
      current = next;
      act(() => {
        for (const listener of listeners) {
          listener();
        }
      });
    },
  };
}
