import type { ApiData, ApiOp, ApiParams } from '../shared/api';
import { apiFailure } from '../shared/bridge';
import type {
  ApiKeyStatus,
  ApiResult,
  ChooseFolderResult,
  FolderOpened,
  ImportTranscriptResult,
  ImportVideoResult,
  PickVideoResult,
  ViewState,
} from '../shared/bridge';

const UNREACHABLE_MESSAGE = "Не вдалося зв'язатися з локальним рушієм. Спробуйте ще раз";

/**
 * `window.clipforge.call` that never rejects: a missing bridge, a refused IPC or
 * an answer the preload could not validate all become an ordinary failed result.
 */
export async function callApi<O extends ApiOp>(
  op: O,
  params: ApiParams[O],
): Promise<ApiResult<ApiData[O]>> {
  try {
    return await window.clipforge.call(op, params);
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/** The failed half of an `ApiResult`. */
export type ApiFailureResult = Extract<ApiResult<never>, { readonly ok: false }>;

/** `window.clipforge.getApiKeyStatus` that never rejects, like `callApi`. */
export async function loadApiKeyStatus(): Promise<ApiResult<ApiKeyStatus>> {
  try {
    return { ok: true, data: await window.clipforge.getApiKeyStatus() };
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/** `window.clipforge.setApiKey` that never rejects. The key goes to main and is not kept here. */
export async function saveApiKey(key: string): Promise<ApiResult<ApiKeyStatus>> {
  try {
    return await window.clipforge.setApiKey(key);
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/** `window.clipforge.forgetApiKey` that never rejects, like `callApi`. */
export async function removeApiKey(): Promise<ApiResult<ApiKeyStatus>> {
  try {
    return await window.clipforge.forgetApiKey();
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/** `window.clipforge.chooseExportFolder` that never rejects, like `callApi`. */
export async function chooseExportFolder(): Promise<ChooseFolderResult | ApiFailureResult> {
  try {
    return await window.clipforge.chooseExportFolder();
  } catch {
    return { ok: false, error: { code: 'BRIDGE_ERROR', message: UNREACHABLE_MESSAGE, retryable: true } };
  }
}

/** `window.clipforge.openExportFolder` that never rejects, like `callApi`. */
export async function openExportFolder(batchId: string): Promise<ApiResult<FolderOpened>> {
  try {
    return await window.clipforge.openExportFolder({ batch_id: batchId });
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/**
 * `window.clipforge.importVideo` that never rejects, like `callApi`. Without `fileToken` main
 * opens the picker; with the token of `pickVideo` it registers that video (ADR 024).
 */
export async function importVideo(projectId: string, fileToken?: string): Promise<ImportVideoResult> {
  try {
    return await window.clipforge.importVideo(
      fileToken === undefined ? { project_id: projectId } : { project_id: projectId, file_token: fileToken },
    );
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/**
 * `window.clipforge.importTranscript` that never rejects, like `callApi`. Main shows the subtitle
 * dialog and reads the file itself; a cancel is `{ canceled: true }` (ADR 039). No key is needed.
 */
export async function importTranscript(sourceId: string): Promise<ImportTranscriptResult> {
  try {
    return await window.clipforge.importTranscript({ source_id: sourceId });
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/** `window.clipforge.pickVideo` that never rejects, like `callApi`. */
export async function pickVideo(): Promise<PickVideoResult> {
  try {
    return await window.clipforge.pickVideo();
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/**
 * `window.clipforge.takeDroppedVideo` that never rejects, like `callApi`. The preload turns the
 * dropped `File` into a path for main only; the answer is the one of `pickVideo` (ADR 024).
 */
export async function takeDroppedVideo(file: File): Promise<PickVideoResult> {
  try {
    return await window.clipforge.takeDroppedVideo(file);
  } catch {
    return apiFailure('BRIDGE_ERROR', UNREACHABLE_MESSAGE, true);
  }
}

/**
 * The project and tab remembered from the last run. Never rejects and never reports a
 * failure: the memory is a convenience, so an unreadable one is the same as none.
 */
export async function loadViewState(): Promise<ViewState | null> {
  try {
    return await window.clipforge.getViewState();
  } catch {
    return null;
  }
}

/** Remembers the project and tab on screen (null forgets them). A failure is ignored. */
export async function saveViewState(state: ViewState | null): Promise<void> {
  try {
    await window.clipforge.setViewState(state);
  } catch {
    // Nothing to tell the user: the next start just opens the project list.
  }
}
