import { useEffect, useState } from 'react';

import type { AppStatus } from '../shared/bridge';

const NAV_ITEMS = [
  { label: 'Проєкти', current: true },
  { label: 'Черга', current: false },
  { label: 'Налаштування', current: false },
] as const;

const POLL_STARTING_MS = 500;
const POLL_READY_MS = 3000;

type StatusView =
  | { readonly kind: 'loading' }
  | { readonly kind: 'status'; readonly status: AppStatus }
  | { readonly kind: 'unavailable' };

/** Follows the backend status; stops after a terminal failure. */
function useStatusView(): StatusView {
  const [view, setView] = useState<StatusView>({ kind: 'loading' });

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async (): Promise<void> => {
      let next: StatusView;
      try {
        next = { kind: 'status', status: await window.clipforge.getStatus() };
      } catch {
        next = { kind: 'unavailable' };
      }
      if (cancelled) {
        return;
      }
      setView(next);
      if (next.kind === 'status' && next.status.state !== 'failed') {
        const delay = next.status.state === 'ready' ? POLL_READY_MS : POLL_STARTING_MS;
        timer = window.setTimeout(() => {
          void poll();
        }, delay);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  return view;
}

function TestBadge() {
  return (
    <span className="badge" data-testid="test-badge">
      Тестові дані
    </span>
  );
}

function StatusScreen({
  failed,
  test,
}: {
  readonly failed: boolean;
  readonly test: boolean;
}) {
  return (
    <div className="status-screen">
      <h1>ClipForge Local</h1>
      {test && <TestBadge />}
      {failed ? (
        <div role="alert" data-testid="app-error" className="status-message status-error">
          <h2>Не вдалося запустити локальний рушій</h2>
          <p>Закрий програму і запусти її знову. Причину збою записано в консоль запуску.</p>
        </div>
      ) : (
        <p role="status" data-testid="app-starting" className="status-message">
          Запускаємо локальний рушій…
        </p>
      )}
    </div>
  );
}

function ReadyShell({ status }: { readonly status: AppStatus }) {
  return (
    <div className="app" data-testid="app-ready">
      <aside className="sidebar">
        <nav aria-label="Основна навігація">
          <ul className="nav-list">
            {NAV_ITEMS.map((item) => (
              <li key={item.label}>
                <button
                  type="button"
                  className="nav-item"
                  aria-current={item.current ? 'page' : undefined}
                  disabled={!item.current}
                >
                  {item.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <p className="note">
          <span aria-hidden="true">ⓘ </span>
          Розділи «Черга» і «Налаштування» ще недоступні.
        </p>
      </aside>
      <div className="content">
        <header className="topbar">
          <h1>ClipForge Local</h1>
          <p className="breadcrumb">Проєкти</p>
          <div className="topbar-status">
            {status.mode === 'test' && <TestBadge />}
            <p className="engine-status" data-testid="engine-status">
              Локальний рушій готовий
              {status.version === null ? '' : ` · версія ${status.version}`}
            </p>
          </div>
        </header>
        <main className="first-run">
          <h2>Знайдімо цікаві моменти у твоєму відео</h2>
          <p className="lead">
            API-ключ можна налаштувати пізніше. Реєстрація і платіжні дані не потрібні.
          </p>
          <div className="actions">
            <button type="button" className="button primary" disabled aria-describedby="engine-note">
              Додати відео
            </button>
            <button type="button" className="button" disabled aria-describedby="engine-note">
              Налаштувати AI
            </button>
          </div>
          <p id="engine-note" className="note">
            <span aria-hidden="true">ⓘ </span>
            Дії стануть доступні після підключення локального рушія.
          </p>
        </main>
      </div>
    </div>
  );
}

export function App() {
  const view = useStatusView();

  if (view.kind === 'status' && view.status.state === 'ready') {
    return <ReadyShell status={view.status} />;
  }
  const test = view.kind === 'status' && view.status.mode === 'test';
  const failed = view.kind === 'unavailable' || (view.kind === 'status' && view.status.state === 'failed');
  return <StatusScreen failed={failed} test={test} />;
}
