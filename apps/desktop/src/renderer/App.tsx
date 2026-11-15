import { useEffect, useState } from 'react';

import type { AppStatus } from '../shared/bridge';
import { useIgnoreStrayDrops } from './DropZone';
import { Shell } from './Shell';
import { TestBadge } from './ui';

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

export function App() {
  const view = useStatusView();
  useIgnoreStrayDrops();

  if (view.kind === 'status' && view.status.state === 'ready') {
    return <Shell status={view.status} />;
  }
  const test = view.kind === 'status' && view.status.mode === 'test';
  const failed = view.kind === 'unavailable' || (view.kind === 'status' && view.status.state === 'failed');
  return <StatusScreen failed={failed} test={test} />;
}
