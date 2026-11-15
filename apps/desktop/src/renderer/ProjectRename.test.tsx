import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ApiData, ApiParams, ProjectDto } from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { App } from './App';
import { PROJECT_ID, failure, installBridge, makeProject, makeProjectItem, ok } from './test-bridge';
import type { ApiHandlers } from './test-bridge';

const ARCHIVED_ID = '11111111-2222-4333-8444-555555555555';
const ARCHIVED_AT = '2026-11-15T08:00:00Z';
const UPDATED_AT = '2026-11-15T09:00:00Z';

type ListAnswer = ApiResult<ApiData['projects.list']>;
type UpdateAnswer = ApiResult<ProjectDto>;

const ACTIVE = makeProjectItem({ name: 'Робочий проєкт', source_count: 2, clip_count: 5 });
const ARCHIVED = makeProjectItem({
  id: ARCHIVED_ID,
  name: 'Старий проєкт',
  archived_at: ARCHIVED_AT,
  updated_at: ARCHIVED_AT,
});

function twoProjects(): ApiHandlers['projects.list'] {
  return (params): ListAnswer =>
    ok({ items: params.include_archived === true ? [ACTIVE, ARCHIVED] : [ACTIVE], next_cursor: null });
}

/** `projects.update` answering a rename as the backend does: the new name, a later updated_at. */
function renaming(): (params: ApiParams['projects.update']) => UpdateAnswer {
  return ({ id, name }) =>
    ok(
      makeProject({
        id,
        name: name ?? ACTIVE.name,
        updated_at: UPDATED_AT,
        archived_at: id === ARCHIVED_ID ? ARCHIVED_AT : null,
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

/** Opens «Ще дії» of the row and chooses «Перейменувати»; the field of the row is what comes back. */
function startRename(row: HTMLElement): HTMLInputElement {
  fireEvent.click(within(row).getByTestId('project-menu'));
  fireEvent.click(within(row).getByTestId('rename-project'));
  return within(row).getByTestId<HTMLInputElement>('rename-input');
}

function typeName(input: HTMLInputElement, value: string): void {
  fireEvent.change(input, { target: { value } });
}

function updateCalls(call: ReturnType<typeof installBridge>['call']): unknown[][] {
  return call.mock.calls.filter(([op]) => op === 'projects.update');
}

describe('Project list: rename', () => {
  it('opens a field in the row with the current name, focused, and sends nothing yet', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');

    const input = startRename(row);

    expect(within(row).getByRole('form', { name: 'Перейменування проєкту' })).toBeVisible();
    expect(input).toHaveValue('Робочий проєкт');
    expect(input).toHaveAccessibleName('Нова назва проєкту');
    expect(input).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(within(row).queryByRole('alert')).not.toBeInTheDocument();
    expect(updateCalls(call)).toHaveLength(0);
  });

  it('is offered for an archived project too', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    fireEvent.click(screen.getByTestId('show-archived'));
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    const row = rowOf('Старий проєкт');

    const input = startRename(row);
    typeName(input, 'Архів 2025');
    fireEvent.submit(within(row).getByTestId('rename-form'));

    await waitFor(() => {
      expect(within(rowOf('Архів 2025')).getByTestId('project-archived')).toBeVisible();
    });
    expect(call).toHaveBeenCalledWith('projects.update', { id: ARCHIVED_ID, name: 'Архів 2025' });
  });

  it('saves on Enter: sends the trimmed name, the list shows it and focus returns to the menu button', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const trigger = within(row).getByTestId('project-menu');
    const input = startRename(row);

    typeName(input, '  Нова назва  ');
    // Enter in the field submits the form.
    fireEvent.submit(within(row).getByTestId('rename-form'));

    await waitFor(() => {
      expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    });
    expect(call).toHaveBeenCalledWith('projects.update', { id: PROJECT_ID, name: 'Нова назва' });
    expect(updateCalls(call)).toHaveLength(1);
    expect(screen.getByTestId('project-item')).toHaveTextContent('Нова назва');
    expect(screen.getByTestId('project-item')).not.toHaveTextContent('Робочий проєкт');
    // The figures of the row are kept.
    expect(within(rowOf('Нова назва')).getByTestId('project-counts')).toHaveTextContent('2 джерела · 5 кліпів');
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('saves with the button «Зберегти»', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const input = startRename(row);

    typeName(input, 'Інша назва');
    expect(within(row).getByRole('button', { name: 'Зберегти' })).toBe(screen.getByTestId('rename-save'));
    fireEvent.click(screen.getByTestId('rename-save'));

    await waitFor(() => {
      expect(screen.getByTestId('project-item')).toHaveTextContent('Інша назва');
    });
    expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    expect(call).toHaveBeenCalledWith('projects.update', { id: PROJECT_ID, name: 'Інша назва' });
    expect(updateCalls(call)).toHaveLength(1);
  });

  it('accepts a name of exactly 120 characters', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const name = '🎬'.repeat(120);

    typeName(startRename(row), name);
    fireEvent.click(screen.getByTestId('rename-save'));

    await waitFor(() => {
      expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    });
    expect(call).toHaveBeenCalledWith('projects.update', { id: PROJECT_ID, name });
  });

  it('closes on Escape without a request and returns focus to the menu button', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const trigger = within(row).getByTestId('project-menu');
    const input = startRename(row);
    typeName(input, 'Не збережу');

    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(screen.getByTestId('project-item')).toHaveTextContent('Робочий проєкт');
    expect(updateCalls(call)).toHaveLength(0);
  });

  it('closes with the button «Скасувати» without a request, and the next time starts from the saved name', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const trigger = within(row).getByTestId('project-menu');
    typeName(startRename(row), 'Не збережу');

    fireEvent.click(screen.getByTestId('rename-cancel'));

    expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(updateCalls(call)).toHaveLength(0);
    expect(startRename(row)).toHaveValue('Робочий проєкт');
  });

  it('refuses an empty name, or one of spaces, in the row and sends nothing', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const input = startRename(row);

    typeName(input, '   ');
    fireEvent.click(screen.getByTestId('rename-save'));

    const alert = within(row).getByRole('alert');
    expect(alert).toHaveTextContent('Введіть назву проєкту');
    expect(alert).toBe(screen.getByTestId('rename-error'));
    expect(input).toBeVisible();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription('Введіть назву проєкту');
    expect(updateCalls(call)).toHaveLength(0);

    typeName(input, '');
    fireEvent.submit(within(row).getByTestId('rename-form'));

    expect(within(row).getByRole('alert')).toHaveTextContent('Введіть назву проєкту');
    expect(updateCalls(call)).toHaveLength(0);
    expect(screen.getByTestId('project-item')).toHaveTextContent('Робочий проєкт');
  });

  it('refuses a name of 121 characters in the row and sends nothing; typing clears the message', async () => {
    const { call } = await openList({ 'projects.list': twoProjects(), 'projects.update': renaming() });
    const row = rowOf('Робочий проєкт');
    const input = startRename(row);

    typeName(input, 'я'.repeat(121));
    fireEvent.submit(within(row).getByTestId('rename-form'));

    expect(within(row).getByRole('alert')).toHaveTextContent('Назва може містити не більше 120 символів');
    expect(input).toBeVisible();
    expect(updateCalls(call)).toHaveLength(0);

    typeName(input, 'я'.repeat(120));

    expect(within(row).queryByRole('alert')).not.toBeInTheDocument();
    expect(input).toHaveAttribute('aria-invalid', 'false');
  });

  it.each([
    ['INVALID_PROJECT_NAME', 'Назва проєкту недопустима', true],
    ['NO_CHANGES', 'Немає змін для збереження', false],
  ])('shows the %s answer of the backend in the row and keeps the field open', async (code, message, invalid) => {
    const { call } = await openList({
      'projects.list': twoProjects(),
      'projects.update': () => failure(code, message),
    });
    const row = rowOf('Робочий проєкт');
    const trigger = within(row).getByTestId('project-menu');
    const input = startRename(row);
    typeName(input, 'Нова назва');

    fireEvent.click(screen.getByTestId('rename-save'));

    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent('Не вдалося перейменувати проєкт.');
    expect(alert).toHaveTextContent(message);
    expect(input).toBeVisible();
    expect(input).toHaveValue('Нова назва');
    expect(input).toHaveAttribute('aria-invalid', String(invalid));
    expect(screen.getByTestId('rename-save')).toBeEnabled();
    expect(trigger).toBeEnabled();
    expect(screen.getByTestId('project-item')).toHaveTextContent('Робочий проєкт');
    expect(updateCalls(call)).toHaveLength(1);
  });

  it('shows any other failure in the row, and a second try with a better name saves', async () => {
    let attempts = 0;
    const { call } = await openList({
      'projects.list': twoProjects(),
      'projects.update': (params): UpdateAnswer => {
        attempts += 1;
        return attempts === 1
          ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
          : renaming()(params);
      },
    });
    const row = rowOf('Робочий проєкт');
    const input = startRename(row);
    typeName(input, 'Нова назва');
    fireEvent.click(screen.getByTestId('rename-save'));

    const alert = await within(row).findByRole('alert');
    expect(alert).toHaveTextContent('Не вдалося перейменувати проєкт. Локальний рушій не відповідає');
    expect(input).toHaveAttribute('aria-invalid', 'false');
    expect(input).toHaveValue('Нова назва');

    fireEvent.click(screen.getByTestId('rename-save'));

    await waitFor(() => {
      expect(screen.queryByTestId('rename-form')).not.toBeInTheDocument();
    });
    expect(screen.getByTestId('project-item')).toHaveTextContent('Нова назва');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(updateCalls(call)).toHaveLength(2);
  });

  it('shows the failure of a bridge that does not answer', async () => {
    await openList({
      'projects.list': twoProjects(),
      'projects.update': () => {
        throw new Error('IPC refused');
      },
    });
    const row = rowOf('Робочий проєкт');
    typeName(startRename(row), 'Нова назва');

    fireEvent.click(screen.getByTestId('rename-save'));

    expect(await within(row).findByRole('alert')).toHaveTextContent("Не вдалося зв'язатися з локальним рушієм");
    expect(screen.getByTestId('rename-input')).toBeVisible();
  });

  it('locks the field, the buttons and the menu while the name is being saved', async () => {
    const { call } = await openList({
      'projects.list': twoProjects(),
      'projects.update': () => new Promise<UpdateAnswer>(() => undefined),
    });
    const row = rowOf('Робочий проєкт');
    const input = startRename(row);
    typeName(input, 'Нова назва');

    fireEvent.click(screen.getByTestId('rename-save'));

    expect(await within(row).findByRole('button', { name: 'Зберігаємо…' })).toBeDisabled();
    expect(screen.getByTestId('rename-cancel')).toBeDisabled();
    expect(input).toHaveAttribute('readonly');
    expect(within(row).getByTestId('project-menu')).toBeDisabled();

    // Neither Escape nor a second submit changes anything: the request is already on its way.
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.submit(within(row).getByTestId('rename-form'));

    expect(screen.getByTestId('rename-form')).toBeVisible();
    expect(updateCalls(call)).toHaveLength(1);
  });
});
