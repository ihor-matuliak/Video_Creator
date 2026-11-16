import { contextBridge, ipcRenderer, webUtils } from 'electron';

import {
  API_CALL_CHANNEL,
  API_KEY_FORGET_CHANNEL,
  API_KEY_SET_CHANNEL,
  API_KEY_STATUS_CHANNEL,
  APP_GET_STATUS_CHANNEL,
  CHOOSE_FOLDER_CHANNEL,
  DROP_VIDEO_CHANNEL,
  EXPORT_OPEN_FOLDER_CHANNEL,
  IMPORT_TRANSCRIPT_CHANNEL,
  IMPORT_VIDEO_CHANNEL,
  PICK_VIDEO_CHANNEL,
  UNSUPPORTED_FILE_MESSAGE,
  VIEW_STATE_GET_CHANNEL,
  VIEW_STATE_SET_CHANNEL,
  apiFailure,
  parseApiCallResult,
  parseApiKeyResult,
  parseApiKeyStatus,
  parseAppStatus,
  parseChooseFolderResult,
  parseGetViewStateResult,
  parseImportTranscriptResult,
  parseImportVideoResult,
  parseOpenFolderResult,
  parsePickVideoResult,
  parseSaveViewStateResult,
} from '../shared/bridge';
import type { ClipforgeBridge } from '../shared/bridge';

// Every answer is validated here again: the renderer never sees unchecked data. No method
// takes or returns a path, token or port of the backend, except the one-time tokens of the
// folder and video the user chose, which name a choice kept in main and are no path. The one
// secret that passes through is the OpenRouter key, and only inwards (`setApiKey`).
const bridge = Object.freeze<ClipforgeBridge>({
  bridgeVersion: 2,
  getStatus: async () => parseAppStatus(await ipcRenderer.invoke(APP_GET_STATUS_CHANNEL)),
  call: async (op, params) =>
    parseApiCallResult(op, await ipcRenderer.invoke(API_CALL_CHANNEL, { op, params })),
  importVideo: async (params) =>
    parseImportVideoResult(await ipcRenderer.invoke(IMPORT_VIDEO_CHANNEL, params)),
  // The file is read and posted in main; the answer is a cancel or a revision, never its path.
  importTranscript: async (params) =>
    parseImportTranscriptResult(await ipcRenderer.invoke(IMPORT_TRANSCRIPT_CHANNEL, params)),
  pickVideo: async () => parsePickVideoResult(await ipcRenderer.invoke(PICK_VIDEO_CHANNEL)),
  // The one place a path is read from a File (ADR 024): it goes to main and nowhere else.
  takeDroppedVideo: async (file) => {
    if (!(file instanceof File)) {
      throw new TypeError('Очікується файл');
    }
    const path = webUtils.getPathForFile(file);
    if (path === '') {
      // Not a file of the disk (a picture dragged from a page, a pasted blob): nothing to send.
      return apiFailure('UNSUPPORTED_FILE', UNSUPPORTED_FILE_MESSAGE);
    }
    return parsePickVideoResult(
      await ipcRenderer.invoke(DROP_VIDEO_CHANNEL, { path }),
      DROP_VIDEO_CHANNEL,
    );
  },
  chooseExportFolder: async () =>
    parseChooseFolderResult(await ipcRenderer.invoke(CHOOSE_FOLDER_CHANNEL)),
  openExportFolder: async (params) =>
    parseOpenFolderResult(await ipcRenderer.invoke(EXPORT_OPEN_FOLDER_CHANNEL, params)),
  getViewState: async () =>
    parseGetViewStateResult(await ipcRenderer.invoke(VIEW_STATE_GET_CHANNEL)),
  setViewState: async (state) =>
    parseSaveViewStateResult(await ipcRenderer.invoke(VIEW_STATE_SET_CHANNEL, state)),
  // The key goes to main on this one channel and no answer carries it back (ADR 034): the
  // parsers keep `{ state, message }` and drop every other key.
  setApiKey: async (key) => {
    if (typeof key !== 'string') {
      throw new TypeError('Очікується рядок');
    }
    return parseApiKeyResult(await ipcRenderer.invoke(API_KEY_SET_CHANNEL, key), API_KEY_SET_CHANNEL);
  },
  forgetApiKey: async () =>
    parseApiKeyResult(await ipcRenderer.invoke(API_KEY_FORGET_CHANNEL), API_KEY_FORGET_CHANNEL),
  getApiKeyStatus: async () => parseApiKeyStatus(await ipcRenderer.invoke(API_KEY_STATUS_CHANNEL)),
});

contextBridge.exposeInMainWorld('clipforge', bridge);
