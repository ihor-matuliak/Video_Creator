import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

import { app, BrowserWindow, ipcMain } from 'electron';

import { APP_GET_STATUS_CHANNEL } from '../shared/bridge';
import { isTrustedSender } from './ipc-guard';
import type { TrustedPage } from './ipc-guard';
import { Sidecar } from './sidecar';
import { resolveDataDir, resolveMode } from './sidecar-protocol';

// Unpackaged layout: <repo>/apps/desktop/out/main/index.js. The packaged sidecar
// bundle is M8 work; until then a missing interpreter simply reports "failed".
const REPO_ROOT = resolve(__dirname, '../../../..');

const mode = resolveMode(process.env);
const sidecar = new Sidecar({
  mode,
  dataDir: resolveDataDir(mode, process.env, app.getPath('userData')),
  pythonPath: join(REPO_ROOT, '.venv', 'Scripts', 'python.exe'),
  cwd: join(REPO_ROOT, 'apps', 'backend'),
  env: process.env,
});

let trustedPage: TrustedPage | null = null;

/**
 * Dev renderer URL is injected by `electron-vite dev`. It is honoured only in
 * an unpackaged app and only for a loopback http address, so a stray
 * environment variable can never point a release build at remote content.
 */
function devRendererUrl(): string | undefined {
  const raw = process.env.ELECTRON_RENDERER_URL;
  if (app.isPackaged || raw === undefined) {
    return undefined;
  }
  try {
    const url = new URL(raw);
    const loopback = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
    return url.protocol === 'http:' && loopback ? url.href : undefined;
  } catch {
    return undefined;
  }
}

// Applies to every web contents ever created, including future ones.
app.on('web-contents-created', (_event, contents) => {
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  contents.on('will-navigate', (event) => {
    event.preventDefault();
  });
  contents.on('will-frame-navigate', (event) => {
    event.preventDefault();
  });
  contents.on('will-redirect', (event) => {
    event.preventDefault();
  });
});

// The only handler: status of the backend, and only for the app's own page.
ipcMain.handle(APP_GET_STATUS_CHANNEL, (event) => {
  const frame = event.senderFrame;
  const allowed = isTrustedSender(
    {
      webContentsId: event.sender.id,
      frameUrl: frame?.url ?? null,
      isMainFrame: frame !== null && frame === event.sender.mainFrame,
    },
    trustedPage,
  );
  if (!allowed) {
    throw new Error('Недозволений відправник IPC');
  }
  return sidecar.getStatus();
});

function createMainWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    show: false,
    title: 'ClipForge Local',
    backgroundColor: '#0F172A',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });

  window.once('ready-to-show', () => {
    window.show();
  });

  const devUrl = devRendererUrl();
  const pageUrl =
    devUrl ?? pathToFileURL(join(__dirname, '../renderer/index.html')).href;
  trustedPage = { webContentsId: window.webContents.id, pageUrl };
  window.on('closed', () => {
    trustedPage = null;
  });

  const loading =
    devUrl === undefined
      ? window.loadFile(join(__dirname, '../renderer/index.html'))
      : window.loadURL(devUrl);
  loading.catch((error: unknown) => {
    console.error('Не вдалося завантажити інтерфейс:', error);
    sidecar.terminateNow();
    app.exit(1);
  });
}

void app.whenReady().then(() => {
  sidecar.start();
  createMainWindow();
});

app.on('window-all-closed', () => {
  app.quit();
});

// Quit waits (bounded) for the sidecar: stdin EOF first, kill of our child only
// as a fallback. The second app.quit() passes straight through.
let shuttingDown = false;
app.on('before-quit', (event) => {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  event.preventDefault();
  sidecar.stop().then(
    () => {
      app.quit();
    },
    (error: unknown) => {
      console.error('Не вдалося зупинити локальний рушій:', error);
      app.quit();
    },
  );
});

process.on('exit', () => {
  sidecar.terminateNow();
});
