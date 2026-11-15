import { contextBridge, ipcRenderer, webUtils } from 'electron';

import {
  API_CALL_CHANNEL,
  APP_GET_STATUS_CHANNEL,
  CHOOSE_FOLDER_CHANNEL,
  DROP_VIDEO_CHANNEL,
  EXPORT_OPEN_FOLDER_CHANNEL,
  IMPORT_VIDEO_CHANNEL,
  PICK_VIDEO_CHANNEL,
  UNSUPPORTED_FILE_MESSAGE,
  VIEW_STATE_GET_CHANNEL,
  VIEW_STATE_SET_CHANNEL,
  apiFailure,
  parseApiCallResult,
  parseAppStatus,
  parseChooseFolderResult,
  parseGetViewStateResult,
  parseImportVideoResult,
  parseOpenFolderResult,
  parsePickVideoResult,
  parseSaveViewStateResult,
} from '../shared/bridge';
import type { ClipforgeBridge } from '../shared/bridge';

// Every answer is validated here again: the renderer never sees unchecked data. No method
// takes or returns a path, token or port of the backend, except the one-time tokens of the
// folder and video the user chose, which name a choice kept in main and are no path.
const bridge = Object.freeze<ClipforgeBridge>({
  bridgeVersion: 2,
  getStatus: async () => parseAppStatus(await ipcRenderer.invoke(APP_GET_STATUS_CHANNEL)),
  call: async (op, params) =>
    parseApiCallResult(op, await ipcRenderer.invoke(API_CALL_CHANNEL, { op, params })),
  importVideo: async (params) =>
    parseImportVideoResult(await ipcRenderer.invoke(IMPORT_VIDEO_CHANNEL, params)),
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
});

contextBridge.exposeInMainWorld('clipforge', bridge);
