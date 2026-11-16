import { useEffect, useState } from 'react';

import type { ProjectDto, SourceDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { callApi, importVideo } from './bridge-calls';
import { DropZone, useVideoDrop } from './DropZone';
import { SourceInspector } from './SourceInspector';
import { StatusText } from './ui';

/** How often a source with an unfinished background hash is asked again. */
const HASH_POLL_MS = 2000;

/**
 * The codes for which the file itself is the problem, so another file, not the same one again, is
 * the way on (docs/03 section 5). Every other code keeps the plain retry.
 */
const UNREADABLE_CODES: ReadonlySet<string> = new Set(['SOURCE_UNREADABLE', 'SOURCE_NO_VIDEO']);

type ImportState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'importing' }
  | { readonly kind: 'canceled' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo };

/**
 * What a registration that did not succeed left behind before the tab opened: the first-run
 * screen opens the project it created even when the video could not be registered (ADR 024).
 */
export type ImportNotice = Extract<ImportState, { readonly kind: 'canceled' | 'error' }>;

export interface SourcesTabProps {
  readonly project: ProjectDto;
  readonly sources: readonly SourceDto[];
  /** Shown when the tab opens, until the user adds a video again. */
  readonly notice?: ImportNotice | undefined;
  /** Hears that the user started a new import, so the opening notice is not shown again. */
  readonly onNoticeHandled?: (() => void) | undefined;
  /** The source shown in the inspector; null until one is chosen or added. */
  readonly selectedSourceId: string | null;
  readonly onSelectSource: (sourceId: string) => void;
  readonly onSourceAdded: (source: SourceDto) => void;
  readonly onSourceRefreshed: (source: SourceDto) => void;
  /** «Створити фрагмент вручну» for the shown source: opens the Моменти tab with the form. */
  readonly onManualClip: (sourceId: string) => void;
}

/** Keeps asking the backend about sources whose SHA-256 is still being computed. */
function useHashRefresh(
  sources: readonly SourceDto[],
  onRefreshed: (source: SourceDto) => void,
): void {
  const pendingKey = sources
    .filter((source) => source.hash_status === 'pending')
    .map((source) => source.id)
    .join(',');

  useEffect(() => {
    if (pendingKey === '') {
      return undefined;
    }
    const ids = pendingKey.split(',');
    let cancelled = false;
    let timer: number | undefined;

    const schedule = (): void => {
      timer = window.setTimeout(() => {
        void Promise.all(ids.map((id) => callApi('sources.get', { id }))).then((results) => {
          if (cancelled) {
            return;
          }
          for (const result of results) {
            if (result.ok) {
              onRefreshed(result.data);
            }
          }
          schedule();
        });
      }, HASH_POLL_MS);
    };

    schedule();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [pendingKey, onRefreshed]);
}

export function SourcesTab({
  project,
  sources,
  notice,
  onNoticeHandled,
  selectedSourceId,
  onSelectSource,
  onSourceAdded,
  onSourceRefreshed,
  onManualClip,
}: SourcesTabProps) {
  const [state, setState] = useState<ImportState>(notice ?? { kind: 'idle' });
  const importing = state.kind === 'importing';
  const unreadable = state.kind === 'error' && UNREADABLE_CODES.has(state.error.code);

  useHashRefresh(sources, onSourceRefreshed);

  /** Registers a video in this project: the one named by `fileToken`, or, without it, the one the picker gives. */
  const register = (fileToken?: string): void => {
    if (importing) {
      return;
    }
    onNoticeHandled?.();
    setState({ kind: 'importing' });
    void importVideo(project.id, fileToken).then((result) => {
      if ('canceled' in result) {
        setState({ kind: 'canceled' });
      } else if (result.ok) {
        onSourceAdded(result.data);
        onSelectSource(result.data.id);
        setState({ kind: 'idle' });
      } else {
        setState({ kind: 'error', error: result.error });
      }
    });
  };

  const startImport = (): void => {
    register();
  };

  // A drop registers the video in this project, as the button does after the picker (ADR 024).
  const drop = useVideoDrop({
    disabled: importing,
    onAttempt: () => {
      onNoticeHandled?.();
      setState({ kind: 'idle' });
    },
    onVideo: (choice) => {
      register(choice.token);
    },
  });

  const selected = sources.find((source) => source.id === selectedSourceId) ?? sources.at(-1);

  return (
    <div className="sources">
      <DropZone drop={drop}>
        <div className="actions">
          <button
            type="button"
            className="button primary"
            data-testid="import-video"
            disabled={importing}
            aria-describedby={importing ? 'import-progress' : undefined}
            onClick={startImport}
          >
            Додати відео
          </button>
        </div>
      </DropZone>
      <p className="note">
        <span aria-hidden="true">ⓘ </span>
        Відео залишається на цьому комп'ютері. Для транскрипції надсилається вибране аудіо.
      </p>

      {state.kind === 'importing' && (
        <p id="import-progress" role="status" data-testid="import-loading" className="note">
          <StatusText tone="neutral">Додаємо відео. Перевірка може тривати кілька секунд…</StatusText>
        </p>
      )}

      {state.kind === 'canceled' && (
        <p role="status" data-testid="import-canceled" className="note">
          <StatusText tone="neutral">Імпорт скасовано. Відео не додано.</StatusText>
        </p>
      )}

      {state.kind === 'error' && (
        <div role="alert" data-testid="import-error" className="notice notice-danger">
          <h3>{unreadable ? 'Не вдалося прочитати відео' : 'Не вдалося додати відео'}</h3>
          <p>{state.error.message}</p>
          <details>
            <summary>Деталі</summary>
            <p>Код помилки: {state.error.code}</p>
          </details>
          <button type="button" className="button" data-testid="import-retry" onClick={startImport}>
            {unreadable ? 'Вибрати інший файл' : 'Спробувати ще раз'}
          </button>
        </div>
      )}

      {sources.length > 1 && (
        <ul className="source-list" aria-label="Джерела проєкту">
          {sources.map((source) => (
            <li key={source.id}>
              <button
                type="button"
                className="source-item"
                data-source-id={source.id}
                aria-current={source.id === selected?.id ? 'true' : undefined}
                onClick={() => {
                  onSelectSource(source.id);
                }}
              >
                {source.display_name}
              </button>
            </li>
          ))}
        </ul>
      )}

      {selected !== undefined && (
        <>
          <SourceInspector source={selected} />
          <div className="actions">
            <button
              type="button"
              className="button"
              data-testid="manual-clip"
              onClick={() => {
                onManualClip(selected.id);
              }}
            >
              Створити фрагмент вручну
            </button>
          </div>
        </>
      )}
    </div>
  );
}
