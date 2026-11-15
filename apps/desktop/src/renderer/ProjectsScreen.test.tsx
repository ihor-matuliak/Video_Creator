import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from './App';
import { failure, installBridge, listOf, makeProject, makeProjectItem, ok } from './test-bridge';
import type { ApiHandlers } from './test-bridge';

async function openApp(handlers: ApiHandlers = {}): Promise<ReturnType<typeof installBridge>> {
  const installed = installBridge({ handlers: { 'projects.list': listOf(), ...handlers } });
  render(<App />);
  await screen.findByTestId('app-ready');
  return installed;
}

function typeName(value: string): void {
  fireEvent.change(screen.getByTestId('project-name'), { target: { value } });
}

describe('Projects screen: list', () => {
  it('shows the empty state with the one «Новий проєкт» action', async () => {
    const { call } = await openApp();

    expect(await screen.findByTestId('projects-empty')).toBeVisible();
    expect(
      screen.getByRole('heading', { level: 3, name: 'Знайдімо цікаві моменти у твоєму відео' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Додай перше відео, щоб почати')).toBeVisible();
    expect(screen.getAllByTestId('new-project')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Новий проєкт' })).toBeVisible();
    expect(call).toHaveBeenCalledWith('projects.list', {});
  });

  it('shows a loading status until the list arrives', async () => {
    installBridge({
      handlers: { 'projects.list': () => new Promise(() => undefined) },
    });
    render(<App />);

    expect(await screen.findByTestId('projects-loading')).toHaveTextContent('Завантажуємо проєкти…');
    expect(screen.queryByTestId('projects-empty')).not.toBeInTheDocument();
  });

  it('lists the projects by name and opens one', async () => {
    const other = makeProject({ id: '11111111-2222-4333-8444-555555555555', name: 'Другий проєкт' });
    await openApp({ 'projects.list': listOf(makeProject(), other) });

    const items = await screen.findAllByTestId('project-item');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Перевірка інтерфейсу');
    expect(items[1]).toHaveTextContent('Другий проєкт');
    expect(screen.queryByTestId('projects-empty')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Другий проєкт/ }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Другий проєкт' })).toBeVisible();
  });

  it('shows the Ukrainian load error with a retry that reloads the list', async () => {
    let calls = 0;
    await openApp({
      'projects.list': () => {
        calls += 1;
        return calls === 1
          ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
          : ok({ items: [makeProjectItem()], next_cursor: null });
      },
    });

    const alert = await screen.findByTestId('projects-error');
    expect(alert).toHaveTextContent('Не вдалося завантажити проєкти');
    expect(alert).toHaveTextContent('Локальний рушій не відповідає');

    fireEvent.click(screen.getByTestId('projects-retry'));

    expect(await screen.findByTestId('project-item')).toHaveTextContent('Перевірка інтерфейсу');
    expect(screen.queryByTestId('projects-error')).not.toBeInTheDocument();
  });

  it('keeps «Новий проєкт» reachable when the list cannot be loaded', async () => {
    await openApp({ 'projects.list': () => failure('SIDECAR_UNAVAILABLE', 'Рушій не відповідає') });

    await screen.findByTestId('projects-error');
    expect(screen.getByTestId('new-project')).toBeVisible();
  });

  it('loads the next page by cursor and appends it', async () => {
    const second = makeProjectItem({ id: '11111111-2222-4333-8444-555555555555', name: 'Сторінка два' });
    const { call } = await openApp({
      'projects.list': (params) =>
        params.cursor === 'next-1'
          ? ok({ items: [second], next_cursor: null })
          : ok({ items: [makeProjectItem()], next_cursor: 'next-1' }),
    });

    fireEvent.click(await screen.findByTestId('projects-more'));

    expect(await screen.findByText('Сторінка два')).toBeVisible();
    expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    expect(screen.queryByTestId('projects-more')).not.toBeInTheDocument();
    expect(call).toHaveBeenCalledWith('projects.list', { cursor: 'next-1' });
  });
});

describe('Projects screen: new project', () => {
  it('opens a name field, creates the project and opens it on the Sources tab', async () => {
    const { call } = await openApp({
      'projects.create': ({ name }) => ok(makeProject({ name })),
    });

    fireEvent.click(await screen.findByTestId('new-project'));
    expect(screen.getByTestId('project-name')).toHaveFocus();
    expect(screen.getByLabelText('Назва проєкту')).toBe(screen.getByTestId('project-name'));

    typeName('Перевірка інтерфейсу');
    fireEvent.click(screen.getByTestId('create-project'));

    expect(await screen.findByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
    expect(call).toHaveBeenCalledWith('projects.create', { name: 'Перевірка інтерфейсу' });
    // The tabs appear once the backend has listed the sources of the project.
    expect(await screen.findByRole('tab', { name: 'Джерела', selected: true })).toBeVisible();
    expect(screen.getByTestId('import-video')).toBeEnabled();
    expect(screen.getByTestId('saved-state')).toHaveTextContent('Збережено');
    expect(screen.queryByTestId('project-name')).not.toBeInTheDocument();
  });

  it('submits with Enter and sends the trimmed name', async () => {
    const { call } = await openApp({
      'projects.create': ({ name }) => ok(makeProject({ name })),
    });

    fireEvent.click(await screen.findByTestId('new-project'));
    typeName('   Мій проєкт  ');
    fireEvent.submit(screen.getByRole('form', { name: 'Новий проєкт' }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Мій проєкт' })).toBeVisible();
    expect(call).toHaveBeenCalledWith('projects.create', { name: 'Мій проєкт' });
  });

  it('refuses an empty or blank name with an inline message and no request', async () => {
    const { call } = await openApp({ 'projects.create': () => ok(makeProject()) });

    fireEvent.click(await screen.findByTestId('new-project'));
    fireEvent.click(screen.getByTestId('create-project'));

    const error = await screen.findByTestId('project-name-error');
    expect(error).toHaveTextContent('Введіть назву проєкту');
    expect(screen.getByTestId('project-name')).toBeInvalid();
    expect(screen.getByTestId('project-name')).toHaveAccessibleDescription(
      expect.stringContaining('Введіть назву проєкту'),
    );

    typeName('   ');
    fireEvent.click(screen.getByTestId('create-project'));

    expect(await screen.findByTestId('project-name-error')).toHaveTextContent('Введіть назву проєкту');
    expect(call).not.toHaveBeenCalledWith('projects.create', expect.anything());
  });

  it('refuses a name longer than 120 characters and counts characters, not code units', async () => {
    const { call } = await openApp({
      'projects.create': ({ name }) => ok(makeProject({ name })),
    });

    fireEvent.click(await screen.findByTestId('new-project'));
    typeName('а'.repeat(121));
    fireEvent.click(screen.getByTestId('create-project'));

    expect(await screen.findByTestId('project-name-error')).toHaveTextContent(
      'Назва може містити не більше 120 символів',
    );
    expect(call).not.toHaveBeenCalledWith('projects.create', expect.anything());

    // 120 astral characters are 240 UTF-16 code units but still a valid 120-character name.
    const exactly120 = '🎬'.repeat(120);
    typeName(exactly120);
    expect(screen.queryByTestId('project-name-error')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('create-project'));

    await screen.findByRole('heading', { level: 2 });
    expect(call).toHaveBeenCalledWith('projects.create', { name: exactly120 });
  });

  it('clears the validation message as soon as the name is edited', async () => {
    await openApp();

    fireEvent.click(await screen.findByTestId('new-project'));
    fireEvent.click(screen.getByTestId('create-project'));
    await screen.findByTestId('project-name-error');

    typeName('Н');

    expect(screen.queryByTestId('project-name-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('project-name')).toBeValid();
  });

  it('shows a backend refusal next to the form and lets the user submit again', async () => {
    let attempts = 0;
    const { call } = await openApp({
      'projects.create': ({ name }) => {
        attempts += 1;
        return attempts === 1
          ? failure('INVALID_PROJECT_NAME', 'Назва проєкту недопустима')
          : ok(makeProject({ name }));
      },
    });

    fireEvent.click(await screen.findByTestId('new-project'));
    typeName('Проєкт');
    fireEvent.click(screen.getByTestId('create-project'));

    const error = await screen.findByTestId('create-project-error');
    expect(error).toHaveTextContent('Не вдалося створити проєкт. Назва проєкту недопустима');
    expect(screen.getByTestId('create-project')).toBeEnabled();
    expect(screen.getByTestId('project-name')).toHaveValue('Проєкт');

    fireEvent.click(screen.getByTestId('create-project'));

    expect(await screen.findByRole('heading', { level: 2, name: 'Проєкт' })).toBeVisible();
    expect(call.mock.calls.filter(([op]) => op === 'projects.create')).toHaveLength(2);
  });

  it('turns a rejected bridge call into a Ukrainian message instead of crashing', async () => {
    await openApp({
      'projects.create': () => {
        throw new Error('IPC refused');
      },
    });

    fireEvent.click(await screen.findByTestId('new-project'));
    typeName('Проєкт');
    fireEvent.click(screen.getByTestId('create-project'));

    expect(await screen.findByTestId('create-project-error')).toHaveTextContent(
      "Не вдалося зв'язатися з локальним рушієм",
    );
  });

  it('closes the form with «Скасувати» and with Escape', async () => {
    await openApp();

    fireEvent.click(await screen.findByTestId('new-project'));
    fireEvent.click(screen.getByRole('button', { name: 'Скасувати' }));
    expect(screen.queryByTestId('project-name')).not.toBeInTheDocument();
    expect(screen.getByTestId('new-project')).toBeVisible();

    fireEvent.click(screen.getByTestId('new-project'));
    fireEvent.keyDown(screen.getByTestId('project-name'), { key: 'Escape' });
    expect(screen.queryByTestId('project-name')).not.toBeInTheDocument();
  });
});

describe('Project view', () => {
  async function openProject(): Promise<void> {
    await openApp({ 'projects.list': listOf(makeProject()) });
    fireEvent.click(await screen.findByTestId('project-item'));
    await screen.findByRole('tablist', { name: 'Розділи проєкту' });
  }

  it('has the three tabs; «Джерела» and «Моменти» work, «Експорт» says why it does not yet', async () => {
    await openProject();

    const tabs = within(screen.getByRole('tablist', { name: 'Розділи проєкту' }));
    expect(tabs.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      'Джерела',
      'Моменти',
      'Експорт',
    ]);
    expect(tabs.getByRole('tab', { name: 'Джерела' })).toHaveAttribute('aria-selected', 'true');
    expect(tabs.getByRole('tab', { name: 'Джерела' })).toBeEnabled();
    expect(tabs.getByRole('tab', { name: 'Моменти' })).toHaveAttribute('aria-selected', 'false');
    expect(tabs.getByRole('tab', { name: 'Моменти' })).toBeEnabled();
    const exportTab = tabs.getByRole('tab', { name: 'Експорт' });
    expect(exportTab).toBeDisabled();
    expect(exportTab).toHaveAccessibleDescription('Розділ «Експорт» ще недоступний.');
    expect(screen.getByRole('tabpanel', { name: 'Джерела' })).toBeVisible();
  });

  it('shows the project name, a breadcrumb and the saved state in the header', async () => {
    await openProject();

    const breadcrumb = screen.getByRole('navigation', { name: 'Навігаційний ланцюжок' });
    expect(within(breadcrumb).getByText('Перевірка інтерфейсу')).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(screen.getByRole('heading', { level: 2, name: 'Перевірка інтерфейсу' })).toBeVisible();
    expect(screen.getByTestId('saved-state')).toHaveTextContent('Збережено');
  });

  it('returns to the project list from the breadcrumb and from the sidebar', async () => {
    await openProject();

    const breadcrumb = screen.getByRole('navigation', { name: 'Навігаційний ланцюжок' });
    fireEvent.click(within(breadcrumb).getByRole('button', { name: 'Проєкти' }));
    expect(await screen.findByRole('heading', { level: 2, name: 'Проєкти' })).toBeVisible();

    fireEvent.click(await screen.findByTestId('project-item'));
    await screen.findByRole('tablist', { name: 'Розділи проєкту' });
    const nav = screen.getByRole('navigation', { name: 'Основна навігація' });
    fireEvent.click(within(nav).getByRole('button', { name: 'Проєкти' }));

    expect(await screen.findByRole('heading', { level: 2, name: 'Проєкти' })).toBeVisible();
  });
});
