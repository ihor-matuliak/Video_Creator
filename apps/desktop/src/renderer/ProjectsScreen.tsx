import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, SubmitEvent } from 'react';

import { PROJECT_SEARCH_MAX_LENGTH } from '../shared/api';
import type { ApiParams, ProjectDto, ProjectListItemDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { callApi } from './bridge-calls';
import { FirstRun } from './FirstRun';
import { ProjectRow } from './ProjectRow';
import { applyProjectUpdate, validateProjectName } from './projects-format';
import type { ImportNotice } from './SourcesTab';

/** How long the search waits for the next keystroke before it asks the backend. */
const SEARCH_DELAY_MS = 300;

type ListState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo }
  | {
      readonly kind: 'ready';
      readonly items: readonly ProjectListItemDto[];
      readonly nextCursor: string | null;
      /**
       * The list is empty because every project is archived and «Показати архівні» is off: it is
       * not the first-run screen, which is for a user with no project at all.
       */
      readonly onlyArchived: boolean;
    };

type MoreState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo };

const MORE_IDLE: MoreState = { kind: 'idle' };

/** What the list shows: the search text, whether archived projects are included, and a retry counter. */
interface ListRequest {
  readonly query: string;
  readonly archived: boolean;
  readonly attempt: number;
}

/** Only what differs from the default goes to the backend: no empty `q`, no `false` flag. */
function listParams(query: string, archived: boolean, cursor?: string): ApiParams['projects.list'] {
  return {
    ...(query === '' ? {} : { q: query }),
    ...(archived ? { include_archived: true } : {}),
    ...(cursor === undefined ? {} : { cursor }),
  };
}

/**
 * The first page of the list for `request`. An empty ordinary list may only be hiding archived
 * projects, so one more question tells «Усі проєкти в архіві» from a user who has none at all.
 */
async function loadFirstPage(request: ListRequest): Promise<ListState> {
  const result = await callApi('projects.list', listParams(request.query, request.archived));
  if (!result.ok) {
    return { kind: 'error', error: result.error };
  }
  const { items, next_cursor: nextCursor } = result.data;
  let onlyArchived = false;
  if (items.length === 0 && nextCursor === null && request.query === '' && !request.archived) {
    const everything = await callApi('projects.list', { include_archived: true, limit: 1 });
    if (!everything.ok) {
      return { kind: 'error', error: everything.error };
    }
    onlyArchived = everything.data.items.length > 0;
  }
  return { kind: 'ready', items, nextCursor, onlyArchived };
}

function NewProjectForm({
  onCreated,
  onCancel,
}: {
  readonly onCreated: (project: ProjectDto) => void;
  readonly onCancel: () => void;
}) {
  const [name, setName] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [failure, setFailure] = useState<ApiErrorInfo | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = (event: SubmitEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (saving) {
      return;
    }
    const problem = validateProjectName(name);
    setFieldError(problem);
    if (problem !== null) {
      return;
    }
    setFailure(null);
    setSaving(true);
    void callApi('projects.create', { name: name.trim() }).then((result) => {
      if (result.ok) {
        onCreated(result.data);
      } else {
        setFailure(result.error);
        setSaving(false);
      }
    });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLFormElement>): void => {
    if (event.key === 'Escape' && !saving) {
      onCancel();
    }
  };

  return (
    <form
      className="panel new-project"
      aria-label="Новий проєкт"
      noValidate
      onSubmit={submit}
      onKeyDown={handleKeyDown}
    >
      <label htmlFor="project-name">Назва проєкту</label>
      <input
        id="project-name"
        data-testid="project-name"
        className="field"
        type="text"
        value={name}
        autoFocus
        autoComplete="off"
        aria-invalid={fieldError !== null}
        aria-describedby={fieldError === null ? undefined : 'project-name-error'}
        onChange={(event) => {
          setName(event.target.value);
          setFieldError(null);
        }}
      />
      {fieldError !== null && (
        <p id="project-name-error" role="alert" data-testid="project-name-error" className="field-error">
          <span aria-hidden="true">✕</span> {fieldError}
        </p>
      )}
      {failure !== null && (
        <p role="alert" data-testid="create-project-error" className="field-error">
          <span aria-hidden="true">✕</span> Не вдалося створити проєкт. {failure.message}
        </p>
      )}
      <div className="actions">
        <button type="submit" className="button primary" data-testid="create-project" disabled={saving}>
          {saving ? 'Створюємо…' : 'Створити'}
        </button>
        <button type="button" className="button" disabled={saving} onClick={onCancel}>
          Скасувати
        </button>
      </div>
    </form>
  );
}

export function ProjectsScreen({
  onOpen,
}: {
  readonly onOpen: (project: ProjectDto, notice?: ImportNotice) => void;
}) {
  const [list, setList] = useState<ListState>({ kind: 'loading' });
  const [more, setMore] = useState<MoreState>(MORE_IDLE);
  const [request, setRequest] = useState<ListRequest>({ query: '', archived: false, attempt: 0 });
  /** What is typed in the search field; `request.query` follows it after SEARCH_DELAY_MS. */
  const [input, setInput] = useState('');
  const [creating, setCreating] = useState(false);
  /** Bumped by every new request, so an answer to «Показати ще» of an earlier one is dropped. */
  const generation = useRef(0);
  const searchField = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the call below awaits.
    const run = { cancelled: false };
    void loadFirstPage(request).then((next) => {
      if (!run.cancelled) {
        setList(next);
      }
    });
    return () => {
      run.cancelled = true;
    };
  }, [request]);

  /** Loads the first page again, with the search text and the archive switch changed as given. */
  const restart = useCallback((change: Partial<Pick<ListRequest, 'query' | 'archived'>>): void => {
    generation.current += 1;
    setList({ kind: 'loading' });
    setMore(MORE_IDLE);
    setRequest((current) => ({ ...current, ...change, attempt: current.attempt + 1 }));
  }, []);

  useEffect(() => {
    const next = input.trim();
    if (next === request.query) {
      return;
    }
    const timer = setTimeout(() => {
      restart({ query: next });
    }, SEARCH_DELAY_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [input, request.query, restart]);

  const reload = (): void => {
    restart({});
  };

  const clearSearch = (): void => {
    setInput('');
    restart({ query: '' });
    searchField.current?.focus();
  };

  const loadMore = (cursor: string): void => {
    const token = generation.current;
    setMore({ kind: 'loading' });
    void callApi('projects.list', listParams(request.query, request.archived, cursor)).then(
      (result) => {
        if (token !== generation.current) {
          return;
        }
        if (!result.ok) {
          setMore({ kind: 'error', error: result.error });
          return;
        }
        setMore(MORE_IDLE);
        setList((current) =>
          current.kind === 'ready'
            ? {
                kind: 'ready',
                items: [...current.items, ...result.data.items],
                nextCursor: result.data.next_cursor,
                onlyArchived: false,
              }
            : current,
        );
      },
    );
  };

  const applyUpdate = (updated: ProjectDto): void => {
    setList((current) => {
      if (current.kind !== 'ready') {
        return current;
      }
      const items = applyProjectUpdate(current.items, updated, request.archived);
      // The last active project was just archived: the archive holds at least that one.
      const onlyArchived =
        items.length === 0 &&
        current.nextCursor === null &&
        request.query === '' &&
        !request.archived &&
        updated.archived_at !== null;
      return { ...current, items, onlyArchived };
    });
  };

  const searching = request.query !== '';
  const nothing = list.kind === 'ready' && list.items.length === 0 && list.nextCursor === null;
  const onlyArchived = list.kind === 'ready' && list.onlyArchived;
  // No project at all, archived ones included: the screen of the first run.
  const firstRun = nothing && !searching && !onlyArchived;

  return (
    <section className="screen" aria-labelledby="projects-title">
      <header className="screen-header">
        <h2 id="projects-title">Проєкти</h2>
        {/* One stable place for the one primary action: it must not jump when the list arrives. */}
        {!creating && (
          <button
            type="button"
            className={firstRun ? 'button' : 'button primary'}
            data-testid="new-project"
            onClick={() => {
              setCreating(true);
            }}
          >
            Новий проєкт
          </button>
        )}
      </header>

      {creating && (
        <NewProjectForm
          onCreated={onOpen}
          onCancel={() => {
            setCreating(false);
          }}
        />
      )}

      <div className="toolbar">
        <div role="search" className="search">
          <input
            ref={searchField}
            type="search"
            className="field"
            data-testid="project-search"
            aria-label="Пошук за назвою"
            placeholder="Пошук за назвою"
            autoComplete="off"
            maxLength={PROJECT_SEARCH_MAX_LENGTH}
            value={input}
            onChange={(event) => {
              setInput(event.target.value);
            }}
          />
        </div>
        <label className="toggle">
          <input
            type="checkbox"
            data-testid="show-archived"
            checked={request.archived}
            onChange={(event) => {
              restart({ archived: event.target.checked });
            }}
          />
          Показати архівні
        </label>
      </div>

      {list.kind === 'loading' && (
        <p role="status" data-testid="projects-loading" className="note">
          Завантажуємо проєкти…
        </p>
      )}

      {list.kind === 'error' && (
        <div role="alert" data-testid="projects-error" className="notice notice-danger">
          <h3>Не вдалося завантажити проєкти</h3>
          <p>{list.error.message}</p>
          <button type="button" className="button" data-testid="projects-retry" onClick={reload}>
            Спробувати ще раз
          </button>
        </div>
      )}

      {nothing && searching && (
        <div className="empty-state" data-testid="projects-no-results">
          <h3>Нічого не знайдено</h3>
          <button
            type="button"
            className="button"
            data-testid="projects-clear-search"
            onClick={clearSearch}
          >
            Очистити пошук
          </button>
        </div>
      )}

      {nothing && !searching && onlyArchived && (
        <div className="empty-state" data-testid="projects-all-archived">
          <h3>Усі проєкти в архіві</h3>
          <p className="lead">Покажи архівні проєкти, щоб відкрити їх або повернути з архіву.</p>
          <button
            type="button"
            className="button primary"
            data-testid="projects-show-archived"
            onClick={() => {
              restart({ archived: true });
            }}
          >
            Показати архівні
          </button>
        </div>
      )}

      {firstRun && (
        // «projects-empty» stays as the id of the empty list; «first-run» is the screen inside it.
        <div className="empty-state" data-testid="projects-empty">
          <FirstRun onOpen={onOpen} />
        </div>
      )}

      {list.kind === 'ready' && list.items.length > 0 && (
        <ul className="project-list" aria-label="Список проєктів">
          {list.items.map((project) => (
            <ProjectRow key={project.id} project={project} onOpen={onOpen} onChanged={applyUpdate} />
          ))}
        </ul>
      )}

      {list.kind === 'ready' && list.nextCursor !== null && (
        <div className="more">
          <button
            type="button"
            className="button"
            data-testid="projects-more"
            disabled={more.kind === 'loading'}
            onClick={() => {
              if (list.nextCursor !== null) {
                loadMore(list.nextCursor);
              }
            }}
          >
            {more.kind === 'loading' ? 'Завантажуємо…' : 'Показати ще'}
          </button>
          {more.kind === 'error' && (
            <div role="alert" data-testid="projects-more-error" className="more-error">
              <p className="field-error">
                <span aria-hidden="true">✕</span> Не вдалося завантажити ще проєкти.{' '}
                {more.error.message}
              </p>
              <button
                type="button"
                className="button"
                data-testid="projects-more-retry"
                onClick={() => {
                  if (list.nextCursor !== null) {
                    loadMore(list.nextCursor);
                  }
                }}
              >
                Спробувати ще раз
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
