import { useEffect, useState } from 'react';

import type { ProjectDto, SystemChecksDto } from '../shared/api';
import type { ApiErrorInfo, VideoChoice } from '../shared/bridge';
import { callApi, importVideo, pickVideo } from './bridge-calls';
import { DropZone, useVideoDrop } from './DropZone';
import { formatFreeSpace, projectNameFromFile } from './first-run-logic';
import type { ImportNotice } from './SourcesTab';
import { StatusText } from './ui';

type ChecksState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly error: ApiErrorInfo }
  | { readonly kind: 'ready'; readonly checks: SystemChecksDto };

/** What «Додати відео» is doing: each step of pick, create the project, register the video. */
type Phase =
  | { readonly kind: 'idle' }
  | { readonly kind: 'picking' }
  | { readonly kind: 'creating' }
  | { readonly kind: 'adding' }
  | { readonly kind: 'error'; readonly title: string; readonly error: ApiErrorInfo };

const IDLE: Phase = { kind: 'idle' };

const PROGRESS_TEXT: Readonly<Record<'picking' | 'creating' | 'adding', string>> = {
  picking: 'Чекаємо вибору відео…',
  creating: 'Створюємо проєкт…',
  adding: 'Додаємо відео. Перевірка може тривати кілька секунд…',
};

const FFMPEG_REQUIRED = 'Для обробки відео потрібен компонент FFmpeg';

/** The readiness of FFmpeg, FFprobe and the data folder, asked of the backend (docs/03 section 3). */
function useSystemChecks(): { readonly state: ChecksState; readonly recheck: () => void } {
  const [state, setState] = useState<ChecksState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the call below awaits.
    const run = { cancelled: false };
    void callApi('system.checks', {}).then((result) => {
      if (!run.cancelled) {
        setState(result.ok ? { kind: 'ready', checks: result.data } : { kind: 'failed', error: result.error });
      }
    });
    return () => {
      run.cancelled = true;
    };
  }, [attempt]);

  const recheck = (): void => {
    setState({ kind: 'loading' });
    setAttempt((current) => current + 1);
  };

  return { state, recheck };
}

function toolText(name: string, check: { readonly ok: boolean; readonly version: string | null }): string {
  if (!check.ok) {
    return `${name} не знайдено`;
  }
  return check.version === null ? `${name} готовий` : `${name} готовий · версія ${check.version}`;
}

function dataDirText(check: SystemChecksDto['data_dir']): string {
  if (!check.ok) {
    return 'Немає доступу до папки даних';
  }
  return check.free_bytes === null
    ? 'Папка даних готова'
    : `Папка даних готова · вільно ${formatFreeSpace(check.free_bytes)}`;
}

function Checks({ checks }: { readonly checks: SystemChecksDto }) {
  return (
    <ul className="checks" aria-label="Готовність до роботи">
      <li>
        <StatusText tone={checks.ffmpeg.ok ? 'success' : 'danger'} testId="system-check-ffmpeg">
          {toolText('FFmpeg', checks.ffmpeg)}
        </StatusText>
      </li>
      <li>
        <StatusText tone={checks.ffprobe.ok ? 'success' : 'danger'} testId="system-check-ffprobe">
          {toolText('FFprobe', checks.ffprobe)}
        </StatusText>
      </li>
      <li>
        <StatusText tone={checks.data_dir.ok ? 'success' : 'danger'} testId="system-check-data-dir">
          {dataDirText(checks.data_dir)}
        </StatusText>
      </li>
    </ul>
  );
}

/**
 * The screen of a user who has no project at all (docs/03 section 3). «Додати відео» picks a
 * video, creates a project named after the file and registers the video in it (ADR 024); the
 * project is opened on «Джерела» whatever the registration answers, with its error if it failed.
 * A video dropped on the zone goes the same way from the second step on.
 */
export function FirstRun({
  onOpen,
}: {
  readonly onOpen: (project: ProjectDto, notice?: ImportNotice) => void;
}) {
  const { state, recheck } = useSystemChecks();
  const [phase, setPhase] = useState<Phase>(IDLE);

  const busy = phase.kind === 'picking' || phase.kind === 'creating' || phase.kind === 'adding';
  // Registering a video needs both tools (FFprobe comes with FFmpeg); until they are known, wait.
  const toolMissing = state.kind === 'ready' && (!state.checks.ffmpeg.ok || !state.checks.ffprobe.ok);
  const waiting = state.kind === 'loading';
  const unavailable = busy || waiting || toolMissing;

  // The notes that say why the button is off: a missing tool, a check still running, a step under way.
  const reasonIds = [
    ...(toolMissing ? ['add-video-reason'] : []),
    ...(waiting ? ['system-checks-status'] : []),
    ...(busy ? ['first-run-progress'] : []),
  ];

  /** The video is chosen, by the picker or by a drop: create the project for it, register it, open it. */
  const register = async (choice: VideoChoice): Promise<void> => {
    setPhase({ kind: 'creating' });
    const created = await callApi('projects.create', { name: projectNameFromFile(choice.fileName) });
    if (!created.ok) {
      setPhase({ kind: 'error', title: 'Не вдалося створити проєкт', error: created.error });
      return;
    }
    setPhase({ kind: 'adding' });
    const imported = await importVideo(created.data.id, choice.token);
    if ('canceled' in imported) {
      onOpen(created.data, { kind: 'canceled' });
    } else if (imported.ok) {
      onOpen(created.data);
    } else {
      // The project stays (there is no delete in M2): it opens with the error and the usual button.
      onOpen(created.data, { kind: 'error', error: imported.error });
    }
  };

  const addVideo = async (): Promise<void> => {
    setPhase({ kind: 'picking' });
    const picked = await pickVideo();
    if ('canceled' in picked) {
      setPhase(IDLE);
      return;
    }
    if ('ok' in picked) {
      setPhase({ kind: 'error', title: 'Не вдалося вибрати відео', error: picked.error });
      return;
    }
    await register(picked);
  };

  // The zone is off exactly when the button is: a drop must not make a project the video cannot go into.
  const drop = useVideoDrop({
    disabled: unavailable,
    onAttempt: () => {
      setPhase(IDLE);
    },
    onVideo: (choice) => {
      void register(choice);
    },
  });

  return (
    <section className="first-run" data-testid="first-run" aria-labelledby="first-run-title">
      <h3 id="first-run-title">Знайдімо цікаві моменти у твоєму відео</h3>
      <p className="lead">Додай перше відео, щоб почати</p>

      <DropZone drop={drop}>
        <div className="actions">
          <button
            type="button"
            className="button primary"
            data-testid="add-video"
            disabled={unavailable}
            aria-describedby={reasonIds.length === 0 ? undefined : reasonIds.join(' ')}
            onClick={() => {
              void addVideo();
            }}
          >
            Додати відео
          </button>
        </div>
      </DropZone>

      {toolMissing && (
        <p id="add-video-reason" className="note" data-testid="add-video-reason">
          <StatusText tone="danger">{FFMPEG_REQUIRED}</StatusText>
        </p>
      )}

      {(phase.kind === 'picking' || phase.kind === 'creating' || phase.kind === 'adding') && (
        <p id="first-run-progress" role="status" data-testid="first-run-progress" className="note">
          <StatusText tone="neutral">{PROGRESS_TEXT[phase.kind]}</StatusText>
        </p>
      )}

      {phase.kind === 'error' && (
        <div role="alert" data-testid="first-run-error" className="notice notice-danger">
          <h3>{phase.title}</h3>
          <p>{phase.error.message}</p>
          <details>
            <summary>Деталі</summary>
            <p>Код помилки: {phase.error.code}</p>
          </details>
        </div>
      )}

      <p className="note">
        Відео залишається на цьому комп'ютері. Створи проєкт кнопкою «Новий проєкт», якщо відео ще
        немає. API-ключ можна налаштувати пізніше, реєстрація і платіжні дані не потрібні.
      </p>

      {state.kind === 'loading' && (
        <p id="system-checks-status" role="status" data-testid="system-checks-loading" className="note">
          <StatusText tone="neutral">Перевіряємо готовність…</StatusText>
        </p>
      )}

      {state.kind === 'failed' && (
        <div role="status" data-testid="system-checks-error" className="note">
          <p>
            <StatusText tone="warning">Не вдалося перевірити готовність. {state.error.message}</StatusText>
          </p>
          <button type="button" className="button" data-testid="system-checks-retry" onClick={recheck}>
            Перевірити ще раз
          </button>
        </div>
      )}

      {state.kind === 'ready' && <Checks checks={state.checks} />}
    </section>
  );
}
