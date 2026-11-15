import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import type { ProjectDto, ProjectListItemDto } from '../shared/api';
import type { ApiErrorInfo } from '../shared/bridge';
import { ActionMenu } from './ActionMenu';
import type { MenuAction } from './ActionMenu';
import { callApi } from './bridge-calls';
import { ProjectRenameForm } from './ProjectRenameForm';
import type { RenameProblem } from './ProjectRenameForm';
import { formatActivity, formatCounts, validateProjectName } from './projects-format';

type Action = 'archive' | 'unarchive';

type RowState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'confirm' }
  | { readonly kind: 'busy'; readonly action: Action }
  | { readonly kind: 'error'; readonly action: Action; readonly error: ApiErrorInfo }
  | {
      readonly kind: 'rename';
      readonly draft: string;
      readonly saving: boolean;
      readonly problem: RenameProblem | null;
    };

const IDLE: RowState = { kind: 'idle' };

const FAILED: Readonly<Record<Action, string>> = {
  archive: 'Не вдалося архівувати проєкт.',
  unarchive: 'Не вдалося повернути проєкт з архіву.',
};

const PROGRESS: Readonly<Record<Action, string>> = {
  archive: 'Архівуємо…',
  unarchive: 'Повертаємо з архіву…',
};

/**
 * One project of the list: the button that opens it and the «Ще дії» menu. The menu renames the
 * project in the row, archives it (after a confirmation that no video on the disk is deleted) or
 * returns it from the archive.
 */
export function ProjectRow({
  project,
  onOpen,
  onChanged,
}: {
  readonly project: ProjectListItemDto;
  readonly onOpen: (project: ProjectDto) => void;
  /** The answer of an archive or an unarchive; the list applies it. */
  readonly onChanged: (updated: ProjectDto) => void;
}) {
  const [state, setState] = useState<RowState>(IDLE);
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const nameId = useId();
  const archived = project.archived_at !== null;

  const run = (action: Action): void => {
    setState({ kind: 'busy', action });
    void callApi('projects.update', { id: project.id, archived: action === 'archive' }).then(
      (result) => {
        if (result.ok) {
          setState(IDLE);
          onChanged(result.data);
        } else {
          setState({ kind: 'error', action, error: result.error });
        }
      },
    );
  };

  const cancelConfirm = (): void => {
    setState(IDLE);
    menuTrigger.current?.focus();
  };

  /**
   * The menu button is disabled while a rename is being saved, so focus can go back to it only
   * once the row is idle again: the effect below does that after the render that enables it.
   */
  const returnFocus = useRef(false);
  useEffect(() => {
    if (state.kind === 'idle' && returnFocus.current) {
      returnFocus.current = false;
      menuTrigger.current?.focus();
    }
  }, [state]);

  const startRename = (): void => {
    setState({ kind: 'rename', draft: project.name, saving: false, problem: null });
  };

  const cancelRename = (): void => {
    returnFocus.current = true;
    setState(IDLE);
  };

  const changeDraft = (draft: string): void => {
    setState((current) => (current.kind === 'rename' ? { ...current, draft, problem: null } : current));
  };

  const submitRename = (): void => {
    if (state.kind !== 'rename' || state.saving) {
      return;
    }
    const invalid = validateProjectName(state.draft);
    if (invalid !== null) {
      setState({ ...state, problem: { message: invalid, invalid: true } });
      return;
    }
    setState({ ...state, saving: true, problem: null });
    void callApi('projects.update', { id: project.id, name: state.draft.trim() }).then((result) => {
      if (result.ok) {
        returnFocus.current = true;
        setState(IDLE);
        onChanged(result.data);
        return;
      }
      const { code, message } = result.error;
      const problem: RenameProblem = {
        message: `Не вдалося перейменувати проєкт. ${message}`,
        invalid: code === 'INVALID_PROJECT_NAME',
      };
      setState((current) => (current.kind === 'rename' ? { ...current, saving: false, problem } : current));
    });
  };

  const handleConfirmKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelConfirm();
    }
  };

  const renameAction: MenuAction = {
    id: 'rename',
    label: 'Перейменувати',
    testId: 'rename-project',
    onSelect: startRename,
  };
  const actions: readonly MenuAction[] = archived
    ? [
        renameAction,
        {
          id: 'unarchive',
          label: 'Повернути з архіву',
          testId: 'unarchive-project',
          onSelect: () => {
            run('unarchive');
          },
        },
      ]
    : [
        renameAction,
        {
          id: 'archive',
          label: 'Архівувати',
          testId: 'archive-project',
          onSelect: () => {
            setState({ kind: 'confirm' });
          },
        },
      ];

  return (
    <li className="project-row">
      <div className="project-line">
        <button
          type="button"
          className="project-item"
          data-testid="project-item"
          data-project-id={project.id}
          onClick={() => {
            onOpen(project);
          }}
        >
          <span className="project-title">
            <span id={nameId} className="project-name">
              {project.name}
            </span>
            {archived && (
              <span className="status status-neutral">
                <span aria-hidden="true">▣</span> <span data-testid="project-archived">В архіві</span>
              </span>
            )}
          </span>
          <span className="project-meta">
            <span data-testid="project-counts">
              {formatCounts(project.source_count, project.clip_count)}
            </span>
            {' · '}
            <span data-testid="project-last-activity">
              Остання дія: {formatActivity(project.last_activity_at)}
            </span>
          </span>
        </button>
        <ActionMenu
          triggerDescribedBy={nameId}
          triggerTestId="project-menu"
          triggerRef={menuTrigger}
          actions={actions}
          disabled={state.kind === 'busy' || (state.kind === 'rename' && state.saving)}
        />
      </div>

      {state.kind === 'rename' && (
        <ProjectRenameForm
          draft={state.draft}
          problem={state.problem}
          saving={state.saving}
          onChange={changeDraft}
          onSubmit={submitRename}
          onCancel={cancelRename}
        />
      )}

      {state.kind === 'confirm' && (
        <div
          role="group"
          aria-label="Підтвердження архівування"
          className="panel project-confirm"
          data-testid="archive-confirm-panel"
          onKeyDown={handleConfirmKeyDown}
        >
          <p className="project-confirm-title">Архівувати проєкт «{project.name}»?</p>
          <p className="note">Відео на диску не видаляються</p>
          <div className="actions">
            <button
              type="button"
              className="button"
              data-testid="archive-confirm"
              onClick={() => {
                run('archive');
              }}
            >
              Архівувати
            </button>
            <button
              type="button"
              className="button"
              data-testid="archive-cancel"
              autoFocus
              onClick={cancelConfirm}
            >
              Скасувати
            </button>
          </div>
        </div>
      )}

      {state.kind === 'busy' && (
        <p role="status" className="note">
          {PROGRESS[state.action]}
        </p>
      )}

      {state.kind === 'error' && (
        <div role="alert" className="notice notice-danger" data-testid="project-action-error">
          <p>
            {FAILED[state.action]} {state.error.message}
          </p>
          <div className="actions">
            <button
              type="button"
              className="button"
              data-testid="project-action-retry"
              onClick={() => {
                run(state.action);
              }}
            >
              Спробувати ще раз
            </button>
            <button
              type="button"
              className="button"
              onClick={() => {
                setState(IDLE);
              }}
            >
              Закрити
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
