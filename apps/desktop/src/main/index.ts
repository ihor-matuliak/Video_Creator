import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

import { app, BrowserWindow, dialog, ipcMain, protocol, shell } from 'electron';

import { APP_GET_STATUS_CHANNEL } from '../shared/bridge';
import { ApiGateway, registerApiIpc } from './api-gateway';
import { isTrustedSender } from './ipc-guard';
import type { TrustedPage } from './ipc-guard';
import { MEDIA_SCHEME_REGISTRATION, registerMediaProtocol } from './media-protocol';
import { Sidecar } from './sidecar';
import { resolveDataDir, resolveMode } from './sidecar-protocol';
import { ViewStateStore, registerViewStateIpc } from './view-state';

// Unpackaged layout: <repo>/apps/desktop/out/main/index.js. The packaged sidecar
// bundle is M8 work; until then a missing interpreter simply reports "failed".
const REPO_ROOT = resolve(__dirname, '../../../..');

// Privileges of a custom scheme can only be granted before the app is ready.
protocol.registerSchemesAsPrivileged([MEDIA_SCHEME_REGISTRATION]);

const mode = resolveMode(process.env);
// The workspace of this run: userData in production, CLIPFORGE_DATA_DIR in test mode. The
// remembered project and tab live next to the database, so a test workspace never touches the
// real one and a relaunch on the same workspace finds its own state.
const dataDir = resolveDataDir(mode, process.env, app.getPath('userData'));
const sidecar = new Sidecar({
  mode,
  dataDir,
  pythonPath: join(REPO_ROOT, '.venv', 'Scripts', 'python.exe'),
  cwd: join(REPO_ROOT, 'apps', 'backend'),
  env: process.env,
});

let trustedPage: TrustedPage | null = null;
let mainWindow: BrowserWindow | null = null;

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

// The renderer's only route to the backend: api:call, dialog:import-video, dialog:pick-video,
// drop:video, export:choose-folder and export:open-folder. Each handler checks the sender; the
// session token and port never leave `sidecar`. `dialog` is passed as the object
// itself so that a replaced `dialog.showOpenDialog` (E2E) is picked up at call time.
registerApiIpc(
  ipcMain,
  new ApiGateway({
    backend: sidecar,
    dialog,
    shell,
    getWindow: () => mainWindow,
    getTrustedPage: () => trustedPage,
  }),
);

// The project and tab shown last, so a restart reopens them (docs/03 section 12).
registerViewStateIpc(ipcMain, new ViewStateStore(dataDir), () => trustedPage);

function createMainWindow(): void {
  const window = new BrowserWindow({
    // Sizes are those of the page, not of the frame around it: the smallest window shows a
    // 1100x720 page, which is the size the layout is checked at (docs/03 sections 8 and 12).
    useContentSize: true,
    width: 1280,
    height: 800,
    minWidth: 1100,
    minHeight: 720,
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
  mainWindow = window;
  window.on('closed', () => {
    trustedPage = null;
    mainWindow = null;
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
  // app-media://source|preview/<uuid4> -> the sidecar's media routes, with Range.
  registerMediaProtocol(protocol, sidecar);
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
