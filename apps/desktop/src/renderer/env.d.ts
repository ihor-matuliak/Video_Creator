/// <reference types="vite/client" />

import type { ClipforgeBridge } from '../shared/bridge';

declare global {
  interface Window {
    readonly clipforge: ClipforgeBridge;
  }
}
