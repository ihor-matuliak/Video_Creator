import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApiData, ApiParams, ProjectDto } from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { App } from './App';
import { PROJECT_ID, failure, installBridge, makeProject, makeProjectItem, ok } from './test-bridge';
import type { ApiHandlers } from './test-bridge';

const ARCHIVED_ID = '11111111-2222-4333-8444-555555555555';
const ARCHIVED_AT = '2026-11-15T08:00:00Z';

type ListAnswer = ApiResult<ApiData['projects.list']>;
type UpdateAnswer = ApiResult<ProjectDto>;

const ACTIVE = makeProjectItem({ name: 'Робочий проєкт', source_count: 2, clip_count: 5 });
const ARCHIVED = makeProjectItem({
  id: ARCHIVED_ID,
  name: 'Старий проєкт',
  archived_at: ARCHIVED_AT,
  updated_at: ARCHIVED_AT,
});

/** The backend of a list with one active and one archived project; the archived one only on request. */
function twoProjects(): ApiHandlers['projects.list'] {
  return (params): ListAnswer =>
    ok({ items: params.include_archived === true ? [ACTIVE, ARCHIVED] : [ACTIVE], next_cursor: null });
}

/** `projects.update` answering as the backend does: archived_at is set or cleared, updated_at moves. */
function updating(): (params: ApiParams['projects.update']) => UpdateAnswer {
  return ({ id, archived }) =>
    ok(
      makeProject({
        id,
        name: id === PROJECT_ID ? ACTIVE.name : ARCHIVED.name,
        updated_at: '2026-11-15T09:00:00Z',
        archived_at: archived === true ? '2026-11-15T09:00:00Z' : null,
      }),
    );
}

async function openList(handlers: ApiHandlers): Promise<ReturnType<typeof installBridge>> {
  const installed = installBridge({ handlers });
  render(<App />);
  await screen.findByTestId('project-search');
  await waitFor(() => {
    expect(screen.queryByTestId('projects-loading')).not.toBeInTheDocument();
  });
  return installed;
}

function rowOf(name: string): HTMLElement {
  const row = screen
    .getAllByTestId('project-item')
    .find((item) => item.textContent.includes(name))
    ?.closest('li');
  if (row === null || row === undefined) {
    throw new Error(`Немає рядка проєкту ${name}`);
  }
  return row;
}

function openMenu(row: HTMLElement): HTMLElement {
  fireEvent.click(within(row).getByTestId('project-menu'));
  return within(row).getByRole('menu', { name: 'Ще дії' });
}

describe('Project list: «Ще дії» menu', () => {
  it('has a closed menu button on each row, described by the project name', async () => {
    await openList({ 'projects.list': twoProjects() });

    const row = rowOf('Робочий проєкт');
    const button = within(row).getByTestId('project-menu');

    expect(button).toHaveAccessibleName('Ще дії');
    expect(button).toHaveAccessibleDescription('Робочий проєкт');
    expect(button).toHaveAttribute('aria-haspopup', 'menu');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(within(row).queryByRole('menu')).not.toBeInTheDocument();
    // The row's own button still opens the project.
    expect(within(row).getByTestId('project-item')).toHaveTextContent('Робочий проєкт');
  });

  it('opens a menu with «Перейменувати» and «Архівувати» and focuses the first', async () => {
    await openList({ 'projects.list': twoProjects() });
    const row = rowOf('Робочий проєкт');

    const menu = openMenu(row);

    expect(within(row).getByTestId('project-menu')).toHaveAttribute('aria-expanded', 'true');
    expect(within(row).getByTestId('project-menu')).toHaveAttribute('aria-controls', menu.id);
    const items = within(menu).getAllByRole('menuitem');
    expect(items.map((item) => item.textContent)).toEqual(['Перейменувати', 'Архівувати']);
    expect(items[0]).toBe(screen.getByTestId('rename-project'));
    expect(items[1]).toBe(screen.getByTestId('archive-project'));
    expect(items[0]).toHaveFocus();
  });

  it('opens with ArrowDown and closes with Escape, which returns focus to the button', async () => {
    await openList({ 'projects.list': twoProjects() });
    const button = within(rowOf('Робочий проєкт')).getByTestId('project-menu');

    fireEvent.keyDown(button, { key: 'ArrowDown' });
    expect(screen.getByRole('menu', { name: 'Ще дії' })).toBeVisible();
    expect(screen.getByRole('menuitem', { name: 'Перейменувати' })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole('menuitem', { name: 'Перейменувати' }), { key: 'Escape' });

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes when the user clicks the button again', async () => {
    await openList({ 'projects.list': twoProjects() });
    const row = rowOf('Робочий проєкт');
    openMenu(row);

    fireEvent.click(within(row).getByTestId('project-menu'));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('Project list: archive', () => {
  it('asks for confirmation first, with the note that no video is deleted, and sends nothing yet', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': updating() });
    const row = rowOf('Робочий проєкт');
    openMenu(row);

    fireEvent.click(screen.getByTestId('archive-project'));

    const confirm = within(row).getByRole('group', { name: 'Підтвердження архівування' });
    expect(confirm).toHaveTextContent('Архівувати проєкт «Робочий проєкт»?');
    expect(within(confirm).getByText('Відео на диску не видаляються')).toBeVisible();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    // The safe choice has the focus.
    expect(within(confirm).getByRole('button', { name: 'Скасувати' })).toHaveFocus();
    expect(call).not.toHaveBeenCalledWith('projects.update', expect.anything());
  });

  it('archives after the confirmation and the project leaves the list', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': updating() });
    openMenu(rowOf('Робочий проєкт'));
    fireEvent.click(screen.getByTestId('archive-project'));

    fireEvent.click(screen.getByTestId('archive-confirm'));

    await waitFor(() => {
      expect(screen.queryByTestId('project-item')).not.toBeInTheDocument();
    });
    expect(call).toHaveBeenCalledWith('projects.update', { id: PROJECT_ID, archived: true });
    expect(call.mock.calls.filter(([op]) => op === 'projects.update')).toHaveLength(1);
    // It was the only active project, so the list is empty now. The project is in the archive, so
    // this is not the first-run screen.
    expect(screen.getByTestId('projects-all-archived')).toBeVisible();
    expect(screen.queryByTestId('first-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('projects-empty')).not.toBeInTheDocument();
  });

  it('cancels with the button, and with Escape, and sends nothing', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': updating() });
    const row = rowOf('Робочий проєкт');
    const trigger = within(row).getByTestId('project-menu');

    openMenu(row);
    fireEvent.click(screen.getByTestId('archive-project'));
    fireEvent.click(screen.getByTestId('archive-cancel'));

    expect(screen.queryByTestId('archive-confirm-panel')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    openMenu(row);
    fireEvent.click(screen.getByTestId('archive-project'));
    fireEvent.keyDown(screen.getByTestId('archive-cancel'), { key: 'Escape' });

    expect(screen.queryByTestId('archive-confirm-panel')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(screen.getByTestId('project-item')).toBeVisible();
    expect(call).not.toHaveBeenCalledWith('projects.update', expect.anything());
  });

  it('disables the menu button while the project is being archived', async () => {
    await openList({
      'projects.list': twoProjects(),
      'projects.update': () => new Promise<UpdateAnswer>(() => undefined),
    });
    const row = rowOf('Робочий проєкт');
    openMenu(row);
    fireEvent.click(screen.getByTestId('archive-project'));

    fireEvent.click(screen.getByTestId('archive-confirm'));

    expect(await within(row).findByText('Архівуємо…')).toBeVisible();
    expect(within(row).getByTestId('project-menu')).toBeDisabled();
    expect(screen.queryByTestId('archive-confirm-panel')).not.toBeInTheDocument();
  });

  it('shows a failed archive with «Спробувати ще раз», which sends it again', async () => {
    let attempts = 0;
    const { call } = await openList({
      'projects.list': twoProjects(),
      'projects.update': (params): UpdateAnswer => {
        attempts += 1;
        return attempts === 1 ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true) : updating()(params);
      },
    });
    openMenu(rowOf('Робочий проєкт'));
    fireEvent.click(screen.getByTestId('archive-project'));
    fireEvent.click(screen.getByTestId('archive-confirm'));

    const alert = await screen.findByTestId('project-action-error');
    expect(alert).toHaveTextContent('Не вдалося архівувати проєкт.');
    expect(alert).toHaveTextContent('Локальний рушій не відповідає');
    expect(screen.getByTestId('project-item')).toBeVisible();

    fireEvent.click(within(alert).getByRole('button', { name: 'Спробувати ще раз' }));

    await waitFor(() => {
      expect(screen.queryByTestId('project-item')).not.toBeInTheDocument();
    });
    expect(call.mock.calls.filter(([op]) => op === 'projects.update')).toHaveLength(2);
  });

  it('closes a failed archive without changing anything', async () => {
    await openList({
      'projects.list': twoProjects(),
      'projects.update': () => failure('SIDECAR_UNAVAILABLE', 'Рушій не відповідає', true),
    });
    openMenu(rowOf('Робочий проєкт'));
    fireEvent.click(screen.getByTestId('archive-project'));
    fireEvent.click(screen.getByTestId('archive-confirm'));
    const alert = await screen.findByTestId('project-action-error');

    fireEvent.click(within(alert).getByRole('button', { name: 'Закрити' }));

    expect(screen.queryByTestId('project-action-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('project-menu')).toBeEnabled();
  });
});

describe('Project list: archived projects', () => {
  it('hides archived projects until «Показати архівні» is on, and then marks them «В архіві»', async () => {
    const { call } = await openList({ 'projects.list': twoProjects() });
    const toggle = screen.getByRole('checkbox', { name: 'Показати архівні' });

    expect(toggle).toBe(screen.getByTestId('show-archived'));
    expect(toggle).not.toBeChecked();
    expect(screen.getAllByTestId('project-item')).toHaveLength(1);
    expect(screen.queryByTestId('project-archived')).not.toBeInTheDocument();

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    expect(toggle).toBeChecked();
    expect(call).toHaveBeenLastCalledWith('projects.list', { include_archived: true });
    const archived = rowOf('Старий проєкт');
    expect(within(archived).getByTestId('project-archived')).toHaveTextContent('В архіві');
    expect(within(rowOf('Робочий проєкт')).queryByTestId('project-archived')).not.toBeInTheDocument();

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(1);
    });
    expect(call).toHaveBeenLastCalledWith('projects.list', {});
  });

  it('offers «Повернути з архіву» for an archived project and «Архівувати» for the others, both with «Перейменувати»', async () => {
    await openList({ 'projects.list': twoProjects() });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });

    const archivedMenu = openMenu(rowOf('Старий проєкт'));

    expect(within(archivedMenu).getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      'Перейменувати',
      'Повернути з архіву',
    ]);
    expect(screen.getByTestId('rename-project')).toHaveFocus();
    expect(screen.queryByTestId('archive-project')).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId('unarchive-project'), { key: 'Escape' });
    const activeMenu = openMenu(rowOf('Робочий проєкт'));

    expect(within(activeMenu).getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      'Перейменувати',
      'Архівувати',
    ]);
  });

  it('returns the project from the archive at once, without a confirmation', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': updating() });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    openMenu(rowOf('Старий проєкт'));

    fireEvent.click(screen.getByTestId('unarchive-project'));

    await waitFor(() => {
      expect(screen.queryByTestId('project-archived')).not.toBeInTheDocument();
    });
    expect(call).toHaveBeenCalledWith('projects.update', { id: ARCHIVED_ID, archived: false });
    expect(screen.queryByTestId('archive-confirm-panel')).not.toBeInTheDocument();
    // It stays in the list, now as an active project that can be archived again.
    expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    openMenu(rowOf('Старий проєкт'));
    expect(screen.getByTestId('archive-project')).toBeVisible();
  });

  it('keeps an archived project in a list that shows archived ones, marked «В архіві»', async () => {
    await openList({ 'projects.list': twoProjects(), 'projects.update': updating() });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    openMenu(rowOf('Робочий проєкт'));
    fireEvent.click(screen.getByTestId('archive-project'));

    fireEvent.click(screen.getByTestId('archive-confirm'));

    await waitFor(() => {
      expect(screen.getAllByTestId('project-archived')).toHaveLength(2);
    });
    expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    // The figures of the row are kept.
    expect(within(rowOf('Робочий проєкт')).getByTestId('project-counts')).toHaveTextContent('2 джерела · 5 кліпів');
  });

  it('shows a failed return from the archive with «Спробувати ще раз»', async () => {
    let attempts = 0;
    await openList({
      'projects.list': twoProjects(),
      'projects.update': (params): UpdateAnswer => {
        attempts += 1;
        return attempts === 1 ? failure('SIDECAR_UNAVAILABLE', 'Рушій не відповідає', true) : updating()(params);
      },
    });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    openMenu(rowOf('Старий проєкт'));
    fireEvent.click(screen.getByTestId('unarchive-project'));

    const alert = await screen.findByTestId('project-action-error');
    expect(alert).toHaveTextContent('Не вдалося повернути проєкт з архіву.');
    expect(alert).toHaveTextContent('Рушій не відповідає');
    expect(screen.getByTestId('project-archived')).toBeVisible();

    fireEvent.click(within(alert).getByRole('button', { name: 'Спробувати ще раз' }));

    await waitFor(() => {
      expect(screen.queryByTestId('project-archived')).not.toBeInTheDocument();
    });
  });

  it('opens an archived project like any other', async () => {
    await openList({ 'projects.list': twoProjects() });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });

    fireEvent.click(within(rowOf('Старий проєкт')).getByTestId('project-item'));

    expect(await screen.findByRole('heading', { level: 2, name: 'Старий проєкт' })).toBeVisible();
  });
});
