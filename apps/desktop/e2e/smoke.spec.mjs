import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { _electron as electron, expect, test } from '@playwright/test';

const ENTRY = resolve(import.meta.dirname, '../out/main/index.js');

/** All processes with their parent and command line (fixed PowerShell script, no shell). */
function listProcesses() {
  const script =
    '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; ' +
    'Get-CimInstance Win32_Process | ' +
    'Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress';
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    windowsHide: true,
  });
  const parsed = JSON.parse(output);
  return Array.isArray(parsed) ? parsed : [parsed];
}

/** Sidecar processes among the descendants of the given Electron main process. */
function sidecarProcesses(electronPid) {
  const all = listProcesses();
  const descendants = new Set([electronPid]);
  for (let grew = true; grew; ) {
    grew = false;
    for (const proc of all) {
      if (descendants.has(proc.ParentProcessId) && !descendants.has(proc.ProcessId)) {
        descendants.add(proc.ProcessId);
        grew = true;
      }
    }
  }
  return all.filter(
    (proc) =>
      descendants.has(proc.ProcessId) &&
      typeof proc.CommandLine === 'string' &&
      proc.CommandLine.includes('clipforge.server'),
  );
}

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === 'EPERM';
  }
}

test('Electron starts the sidecar, shows live test status and ends it on quit', async () => {
  expect(existsSync(ENTRY), `build the app first: ${ENTRY}`).toBe(true);
  const dataDir = mkdtempSync(join(tmpdir(), 'clipforge-smoke-'));
  const env = { ...process.env, CLIPFORGE_E2E: '1', CLIPFORGE_DATA_DIR: dataDir };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.CLIPFORGE_MODE;

  const app = await electron.launch({ args: [ENTRY], env });
  let sidecar = [];
  try {
    const page = await app.firstWindow();

    await expect(page.getByTestId('app-ready')).toBeVisible();
    await expect(page.getByText('Тестові дані')).toBeVisible();
    await expect(page.getByTestId('engine-status')).toContainText('Локальний рушій готовий');
    await expect(page.getByTestId('app-error')).toHaveCount(0);

    // The bridge is narrow: no token, no raw fetch, no Node.
    const surface = await page.evaluate(async () => {
      const bridge = globalThis.clipforge;
      const status = await bridge.getStatus();
      return {
        keys: Object.keys(bridge).sort(),
        statusKeys: Object.keys(status).sort(),
        status,
        serialized: JSON.stringify({ keys: Object.keys(bridge), status }),
        hasNode: typeof globalThis.require !== 'undefined' || typeof globalThis.process !== 'undefined',
      };
    });
    expect(surface.keys).toEqual(['bridgeVersion', 'getStatus']);
    expect(surface.statusKeys).toEqual(['mode', 'state', 'version']);
    expect(surface.status).toMatchObject({ state: 'ready', mode: 'test' });
    expect(surface.serialized).not.toMatch(/token|fetch/i);
    expect(surface.serialized).not.toMatch(/[0-9a-f]{32,}/i);
    expect(surface.hasNode).toBe(false);

    // The sidecar really runs as a child of this Electron instance.
    const electronPid = app.process().pid;
    expect(electronPid).toBeGreaterThan(0);
    sidecar = sidecarProcesses(electronPid);
    expect(sidecar.length).toBeGreaterThan(0);
    expect(sidecar.every((proc) => isAlive(proc.ProcessId))).toBe(true);
    for (const proc of sidecar) {
      // The token travels through stdin only, never through argv.
      expect(proc.CommandLine).not.toMatch(/[0-9a-f]{32,}/i);
      expect(proc.CommandLine).toContain('--config-stdin');
    }
  } finally {
    await app.close();
  }

  await expect
    .poll(() => sidecar.filter((proc) => isAlive(proc.ProcessId)).map((proc) => proc.ProcessId), {
      message: 'sidecar processes must be gone after app.close()',
      timeout: 15_000,
    })
    .toEqual([]);

  rmSync(dataDir, { recursive: true, force: true });
});
