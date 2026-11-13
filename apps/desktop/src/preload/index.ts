import { contextBridge, ipcRenderer } from 'electron';

import { APP_GET_STATUS_CHANNEL, parseAppStatus } from '../shared/bridge';
import type { ClipforgeBridge } from '../shared/bridge';

const bridge: ClipforgeBridge = Object.freeze({
  bridgeVersion: 1,
  getStatus: async () => parseAppStatus(await ipcRenderer.invoke(APP_GET_STATUS_CHANNEL)),
});

contextBridge.exposeInMainWorld('clipforge', bridge);
