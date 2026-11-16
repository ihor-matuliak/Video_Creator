import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { AppStatus, ClipforgeBridge } from '../shared/bridge';
import { App } from './App';
import { READY, installBridge, listOf } from './test-bridge';

const NO_PROJECTS = { 'projects.list': listOf() };

function installStatus(status: AppStatus): void {
  installBridge({ getStatus: () => Promise.resolve(status), handlers: NO_PROJECTS });
}

describe('App shell when the backend is ready', () => {
  it('shows the ClipForge Local title and the ready marker', async () => {
    installStatus(READY);
    render(<App />);

    expect(await screen.findByTestId('app-ready')).toBeVisible();
    expect(screen.getByRole('heading', { level: 1, name: 'ClipForge Local' })).toBeInTheDocument();
  });

  it('lists the sidebar sections with Projects as the current page', async () => {
    installStatus(READY);
    render(<App />);

    const nav = await screen.findByRole('navigation', { name: 'Основна навігація' });
    expect(within(nav).getByRole('button', { name: 'Проєкти' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).getByRole('button', { name: 'Черга' })).toBeDisabled();
    expect(within(nav).getByRole('button', { name: 'Налаштування' })).toBeEnabled();
  });

  it('explains why the Queue is unavailable, next to its button', async () => {
    installStatus(READY);
    render(<App />);
    const nav = await screen.findByRole('navigation', { name: 'Основна навігація' });

    expect(within(nav).getByRole('button', { name: 'Черга' })).toHaveAccessibleDescription(
      'Розділ «Черга» ще недоступний.',
    );
    expect(within(nav).getByRole('button', { name: 'Налаштування' })).not.toHaveAttribute(
      'aria-describedby',
    );
  });

  it('collapses the sidebar and expands it again without losing the labels', async () => {
    installStatus(READY);
    render(<App />);
    const sidebar = await screen.findByTestId('sidebar');
    const toggle = screen.getByTestId('sidebar-toggle');

    expect(sidebar).toHaveAttribute('data-collapsed', 'false');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveAccessibleName('Згорнути панель');

    fireEvent.click(toggle);

    expect(sidebar).toHaveAttribute('data-collapsed', 'true');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAccessibleName('Розгорнути панель');
    // Labels stay in the accessibility tree, so a collapsed sidebar is still operable.
    expect(within(sidebar).getByRole('button', { name: 'Проєкти' })).toBeInTheDocument();

    fireEvent.click(toggle);

    expect(sidebar).toHaveAttribute('data-collapsed', 'false');
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
    installBridge({ getStatus: () => new Promise<AppStatus>(() => undefined) });
    render(<App />);

    expect(screen.getByTestId('app-starting')).toBeVisible();
    expect(screen.queryByTestId('app-ready')).not.toBeInTheDocument();
  });

  it('moves from starting to ready by polling the status', async () => {
    const getStatus = vi
      .fn<ClipforgeBridge['getStatus']>()
      .mockResolvedValueOnce({ state: 'starting', mode: 'production', version: null })
      .mockResolvedValue(READY);
    installBridge({ getStatus, handlers: NO_PROJECTS });
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
    installBridge({ getStatus: () => Promise.reject(new Error('IPC refused')) });
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
    installBridge({ getStatus });
    render(<App />);

    await screen.findByTestId('app-error');
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(getStatus).toHaveBeenCalledTimes(1);
  });
});
