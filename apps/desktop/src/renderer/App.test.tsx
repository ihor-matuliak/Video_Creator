import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AppStatus, ClipforgeBridge } from '../shared/bridge';
import { App } from './App';

const READY: AppStatus = { state: 'ready', mode: 'production', version: '0.1.0' };

function installBridge(getStatus: ClipforgeBridge['getStatus']): void {
  const bridge: ClipforgeBridge = { bridgeVersion: 1, getStatus };
  Object.defineProperty(window, 'clipforge', { value: bridge, configurable: true });
}

function installStatus(status: AppStatus): void {
  installBridge(() => Promise.resolve(status));
}

afterEach(() => {
  Reflect.deleteProperty(window, 'clipforge');
});

describe('App shell when the backend is ready', () => {
  it('shows the ClipForge Local title and the ready marker', async () => {
    installStatus(READY);
    render(<App />);

    expect(await screen.findByTestId('app-ready')).toBeVisible();
    expect(screen.getByRole('heading', { level: 1, name: 'ClipForge Local' })).toBeInTheDocument();
  });

  it('shows the Ukrainian first-run heading', async () => {
    installStatus(READY);
    render(<App />);

    expect(
      await screen.findByRole('heading', {
        level: 2,
        name: 'Знайдімо цікаві моменти у твоєму відео',
      }),
    ).toBeInTheDocument();
  });

  it('lists the sidebar sections with Projects as the current page', async () => {
    installStatus(READY);
    render(<App />);

    expect(
      await screen.findByRole('navigation', { name: 'Основна навігація' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Проєкти' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'Черга' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Налаштування' })).toBeDisabled();
  });

  it('keeps unwired actions disabled with a visible explanation', async () => {
    installStatus(READY);
    render(<App />);
    await screen.findByTestId('app-ready');

    for (const name of ['Додати відео', 'Налаштувати AI']) {
      const button = screen.getByRole('button', { name });
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(
        'Дії стануть доступні після підключення локального рушія.',
      );
    }
  });

  it('shows the backend version and no test badge in production mode', async () => {
    installStatus(READY);
    render(<App />);

    expect(await screen.findByTestId('engine-status')).toHaveTextContent(
      'Локальний рушій готовий · версія 0.1.0',
    );
    expect(screen.queryByText('Тестові дані')).not.toBeInTheDocument();
  });

  it('shows the «Тестові дані» badge in test mode', async () => {
    installStatus({ state: 'ready', mode: 'test', version: '0.1.0' });
    render(<App />);

    await screen.findByTestId('app-ready');
    expect(screen.getByText('Тестові дані')).toBeVisible();
  });
});

describe('App shell before the backend is ready', () => {
  it('shows a starting message and no ready marker while the backend starts', async () => {
    installStatus({ state: 'starting', mode: 'production', version: null });
    render(<App />);

    expect(await screen.findByTestId('app-starting')).toHaveTextContent(
      'Запускаємо локальний рушій…',
    );
    expect(screen.queryByTestId('app-ready')).not.toBeInTheDocument();
  });

  it('shows no ready marker before the first status arrives', () => {
    installBridge(() => new Promise<AppStatus>(() => undefined));
    render(<App />);

    expect(screen.getByTestId('app-starting')).toBeVisible();
    expect(screen.queryByTestId('app-ready')).not.toBeInTheDocument();
  });

  it('moves from starting to ready by polling the status', async () => {
    const getStatus = vi
      .fn<ClipforgeBridge['getStatus']>()
      .mockResolvedValueOnce({ state: 'starting', mode: 'production', version: null })
      .mockResolvedValue(READY);
    installBridge(getStatus);
    render(<App />);

    await screen.findByTestId('app-starting');
    expect(await screen.findByTestId('app-ready', {}, { timeout: 3000 })).toBeVisible();
    expect(screen.queryByTestId('app-starting')).not.toBeInTheDocument();
  });

  it('keeps the test badge visible while starting in test mode', async () => {
    installStatus({ state: 'starting', mode: 'test', version: null });
    render(<App />);

    await screen.findByTestId('app-starting');
    expect(screen.getByText('Тестові дані')).toBeVisible();
  });
});

describe('App shell when the backend fails', () => {
  it('shows a Ukrainian error state and never the ready marker', async () => {
    installStatus({ state: 'failed', mode: 'production', version: null });
    render(<App />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Не вдалося запустити локальний рушій');
    expect(screen.queryByTestId('app-ready')).not.toBeInTheDocument();
  });

  it('shows the error state when the status cannot be read', async () => {
    installBridge(() => Promise.reject(new Error('IPC refused')));
    render(<App />);

    expect(await screen.findByTestId('app-error')).toBeVisible();
    expect(screen.queryByTestId('app-ready')).not.toBeInTheDocument();
  });

  it('shows the error state when the bridge is missing', async () => {
    render(<App />);

    expect(await screen.findByTestId('app-error')).toBeVisible();
  });

  it('stops polling after a failure', async () => {
    const getStatus = vi
      .fn<ClipforgeBridge['getStatus']>()
      .mockResolvedValue({ state: 'failed', mode: 'production', version: null });
    installBridge(getStatus);
    render(<App />);

    await screen.findByTestId('app-error');
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(getStatus).toHaveBeenCalledTimes(1);
  });
});
