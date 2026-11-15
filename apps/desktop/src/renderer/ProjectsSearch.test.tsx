import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PROJECT_SEARCH_MAX_LENGTH } from '../shared/api';
import type { ApiData } from '../shared/api';
import type { ApiResult } from '../shared/bridge';
import { App } from './App';
import { failure, installBridge, listOf, makeProjectItem, ok } from './test-bridge';
import type { ApiHandlers } from './test-bridge';

const OTHER_ID = '11111111-2222-4333-8444-555555555555';

/** Opens the app on the project list and waits until its first answer is on screen. */
async function openList(handlers: ApiHandlers): Promise<ReturnType<typeof installBridge>> {
  const installed = installBridge({ handlers });
  render(<App />);
  await screen.findByTestId('project-search');
  await waitFor(() => {
    expect(screen.queryByTestId('projects-loading')).not.toBeInTheDocument();
  });
  installed.call.mockClear();
  return installed;
}

function searchField(): HTMLElement {
  return screen.getByTestId('project-search');
}

function type(value: string): void {
  fireEvent.change(searchField(), { target: { value } });
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type ListAnswer = ApiResult<ApiData['projects.list']>;

/** The backend of a list that holds two projects and finds them by a lower-case match, like the real one. */
function searchable(): ApiHandlers['projects.list'] {
  const all = [
    makeProjectItem({ name: 'Кирилиця Тест' }),
    makeProjectItem({ id: OTHER_ID, name: 'Другий проєкт' }),
  ];
  return (params): ListAnswer => {
    const needle = (params.q ?? '').trim().toLowerCase();
    return ok({
      items: all.filter((item) => item.name.toLowerCase().includes(needle)),
      next_cursor: null,
    });
  };
}

describe('Project list: search', () => {
  it('has a search field named «Пошук за назвою» and asks for the whole list at first', async () => {
    const { call } = await openList({ 'projects.list': listOf(makeProjectItem()) });

    expect(screen.getByRole('searchbox', { name: 'Пошук за назвою' })).toBe(searchField());
    expect(searchField()).toHaveValue('');
    expect(searchField()).toHaveAttribute('maxlength', String(PROJECT_SEARCH_MAX_LENGTH));
    expect(call).not.toHaveBeenCalled();
  });

  it('asks the backend only after the typing pauses, with the trimmed text', async () => {
    const { call } = await openList({ 'projects.list': searchable() });

    type('  Кир  ');
    expect(call).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(call).toHaveBeenCalledWith('projects.list', { q: 'Кир' });
    });
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('sends only the last text when the user keeps typing', async () => {
    const { call } = await openList({ 'projects.list': searchable() });

    type('к');
    await pause(100);
    type('ки');
    await pause(100);
    type('кир');

    await waitFor(() => {
      expect(call).toHaveBeenCalledWith('projects.list', { q: 'кир' });
    });
    expect(call).toHaveBeenCalledTimes(1);
  });

  it('finds a Cyrillic name whatever the case of the typed text and shows only the matches', async () => {
    const { call } = await openList({ 'projects.list': searchable() });
    expect(await screen.findAllByTestId('project-item')).toHaveLength(2);

    type('КИРИЛИЦЯ');

    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(1);
    });
    expect(screen.getByTestId('project-item')).toHaveTextContent('Кирилиця Тест');
    // The case is not changed on the way: the backend compares without it.
    expect(call).toHaveBeenCalledWith('projects.list', { q: 'КИРИЛИЦЯ' });
  });

  it('does not ask again for a text that only differs by spaces from the one on screen', async () => {
    const { call } = await openList({ 'projects.list': searchable() });

    type('   ');
    await pause(450);

    expect(call).not.toHaveBeenCalled();
  });

  it('shows «Нічого не знайдено» with «Очистити пошук», which restores the whole list', async () => {
    const { call } = await openList({ 'projects.list': searchable() });

    type('немає такого');

    const nothing = await screen.findByTestId('projects-no-results');
    expect(within(nothing).getByRole('heading', { level: 3, name: 'Нічого не знайдено' })).toBeVisible();
    // This is not the first-run screen: the user does have projects.
    expect(screen.queryByTestId('projects-empty')).not.toBeInTheDocument();
    expect(screen.queryByTestId('project-item')).not.toBeInTheDocument();

    fireEvent.click(within(nothing).getByRole('button', { name: 'Очистити пошук' }));

    expect(await screen.findAllByTestId('project-item')).toHaveLength(2);
    expect(searchField()).toHaveValue('');
    expect(searchField()).toHaveFocus();
    expect(screen.queryByTestId('projects-no-results')).not.toBeInTheDocument();
    expect(call).toHaveBeenLastCalledWith('projects.list', {});
  });

  it('goes back to the whole list when the field is emptied by hand', async () => {
    const { call } = await openList({ 'projects.list': searchable() });

    type('другий');
    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(1);
    });
    type('');

    await waitFor(() => {
      expect(screen.getAllByTestId('project-item')).toHaveLength(2);
    });
    expect(call).toHaveBeenLastCalledWith('projects.list', {});
  });

  it('shows the first-run screen, not «Нічого не знайдено», when there are no projects at all', async () => {
    await openList({ 'projects.list': listOf() });

    expect(screen.getByTestId('projects-empty')).toBeVisible();
    expect(screen.queryByTestId('projects-no-results')).not.toBeInTheDocument();
  });

  it('shows a failed search with «Спробувати ще раз», which repeats the same search', async () => {
    let calls = 0;
    const { call } = await openList({
      'projects.list': (params): ListAnswer => {
        if (params.q === undefined) {
          return ok({ items: [makeProjectItem()], next_cursor: null });
        }
        calls += 1;
        return calls === 1
          ? failure('SIDECAR_UNAVAILABLE', 'Локальний рушій не відповідає', true)
          : ok({ items: [makeProjectItem({ name: 'Знайдений' })], next_cursor: null });
      },
    });

    type('знайд');

    const alert = await screen.findByTestId('projects-error');
    expect(alert).toHaveTextContent('Не вдалося завантажити проєкти');
    expect(alert).toHaveTextContent('Локальний рушій не відповідає');
    // The field keeps what the user typed, so the search can be fixed or cleared.
    expect(searchField()).toHaveValue('знайд');

    fireEvent.click(within(alert).getByRole('button', { name: 'Спробувати ще раз' }));

    expect(await screen.findByTestId('project-item')).toHaveTextContent('Знайдений');
    expect(screen.queryByTestId('projects-error')).not.toBeInTheDocument();
    expect(call).toHaveBeenLastCalledWith('projects.list', { q: 'знайд' });
  });

  it('keeps the search text for the next page and for the archive switch', async () => {
    const { call } = await openList({
      'projects.list': (params): ListAnswer =>
        params.cursor === 'next-1'
          ? ok({ items: [makeProjectItem({ id: OTHER_ID, name: 'Кир другий' })], next_cursor: null })
          : ok({ items: [makeProjectItem({ name: 'Кир перший' })], next_cursor: 'next-1' }),
    });

    type('кир');
    await waitFor(() => {
      expect(call).toHaveBeenCalledWith('projects.list', { q: 'кир' });
    });
    fireEvent.click(await screen.findByTestId('projects-more'));

    expect(await screen.findByText('Кир другий')).toBeVisible();
    expect(call).toHaveBeenCalledWith('projects.list', { q: 'кир', cursor: 'next-1' });

    fireEvent.click(screen.getByTestId('show-archived'));

    await waitFor(() => {
      expect(call).toHaveBeenLastCalledWith('projects.list', { q: 'кир', include_archived: true });
    });
    expect(searchField()).toHaveValue('кир');
  });

  it('drops the answer to «Показати ще» of a search that has been replaced', async () => {
    let release: (answer: ListAnswer) => void = () => undefined;
    await openList({
      'projects.list': (params) => {
        if (params.cursor === 'next-1') {
          return new Promise<ListAnswer>((resolve) => {
            release = resolve;
          });
        }
        return params.q === 'нове'
          ? ok({ items: [makeProjectItem({ name: 'Нове' })], next_cursor: null })
          : ok({ items: [makeProjectItem({ name: 'Старе' })], next_cursor: 'next-1' });
      },
    });
    fireEvent.click(await screen.findByTestId('projects-more'));

    type('нове');
    expect(await screen.findByText('Нове')).toBeVisible();
    release(ok({ items: [makeProjectItem({ id: OTHER_ID, name: 'Запізніла сторінка' })], next_cursor: null }));
    await pause(50);

    expect(screen.queryByText('Запізніла сторінка')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('project-item')).toHaveLength(1);
  });

  it('shows a failed «Показати ще» with «Спробувати ще раз»', async () => {
    let attempts = 0;
    await openList({
      'projects.list': (params): ListAnswer => {
        if (params.cursor === undefined) {
          return ok({ items: [makeProjectItem()], next_cursor: 'next-1' });
        }
        attempts += 1;
        return attempts === 1
          ? failure('SIDECAR_UNAVAILABLE', 'Рушій не відповідає', true)
          : ok({ items: [makeProjectItem({ id: OTHER_ID, name: 'Сторінка два' })], next_cursor: null });
      },
    });

    fireEvent.click(await screen.findByTestId('projects-more'));

    const alert = await screen.findByTestId('projects-more-error');
    expect(alert).toHaveTextContent('Не вдалося завантажити ще проєкти');
    expect(alert).toHaveTextContent('Рушій не відповідає');

    fireEvent.click(within(alert).getByRole('button', { name: 'Спробувати ще раз' }));

    expect(await screen.findByText('Сторінка два')).toBeVisible();
    expect(screen.queryByTestId('projects-more-error')).not.toBeInTheDocument();
  });
});

describe('Project list: counts and last activity', () => {
  it('shows «N джерел · M кліпів · Остання дія: дд.мм.рррр гг:хх» in the local time', async () => {
    await openList({
      'projects.list': listOf(
        makeProjectItem({
          source_count: 3,
          clip_count: 1,
          last_activity_at: new Date(2026, 10, 4, 9, 5).toISOString(),
        }),
      ),
    });

    const item = await screen.findByTestId('project-item');
    expect(within(item).getByTestId('project-counts')).toHaveTextContent(/^3 джерела · 1 кліп$/);
    expect(within(item).getByTestId('project-last-activity')).toHaveTextContent(
      /^Остання дія: 04\.11\.2026 09:05$/,
    );
    expect(item).toHaveTextContent('3 джерела · 1 кліп · Остання дія: 04.11.2026 09:05');
  });

  it('uses the Ukrainian plural forms for each project', async () => {
    await openList({
      'projects.list': listOf(
        makeProjectItem({ name: 'Нуль', source_count: 0, clip_count: 0 }),
        makeProjectItem({ id: OTHER_ID, name: 'Багато', source_count: 21, clip_count: 12 }),
      ),
    });

    const counts = (await screen.findAllByTestId('project-counts')).map((node) => node.textContent);

    expect(counts).toEqual(['0 джерел · 0 кліпів', '21 джерело · 12 кліпів']);
  });

  it('shows «невідомо» for a last activity the backend did not date properly', async () => {
    await openList({ 'projects.list': listOf(makeProjectItem({ last_activity_at: 'учора' })) });

    expect(await screen.findByTestId('project-last-activity')).toHaveTextContent('Остання дія: невідомо');
  });
});
