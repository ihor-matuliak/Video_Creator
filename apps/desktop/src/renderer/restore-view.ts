import type { ProjectDto } from '../shared/api';
import type { PanelLayout, RememberedTab } from '../shared/bridge';
import { callApi, loadViewState, saveViewState } from './bridge-calls';

/** A guard against a backend that keeps returning a cursor. */
const MAX_PAGES = 100;

export interface RestoredView {
  readonly project: ProjectDto;
  readonly tab: RememberedTab;
  readonly layout: PanelLayout;
}

/**
 * The project, tab and panel widths of the last run, or null for the project list (docs/03 section 12).
 *
 * There is no "get one project" call, so the project is looked up in the list of projects.
 * One that is not there any more (deleted, archived, or a workspace that was replaced) is
 * not an error: the remembered state is dropped and the list is shown. A list that cannot be
 * read right now also gives null, but keeps the memory for the next start; the project list
 * then reports its own problem.
 */
export async function restoreLastView(): Promise<RestoredView | null> {
  const remembered = await loadViewState();
  if (remembered === null) {
    return null;
  }
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('projects.list', cursor === undefined ? {} : { cursor });
    if (!result.ok) {
      return null;
    }
    const project = result.data.items.find((item) => item.id === remembered.project_id);
    if (project !== undefined) {
      return { project, tab: remembered.tab, layout: remembered.layout };
    }
    if (result.data.next_cursor === null) {
      // The whole list was read and the project is not in it.
      await saveViewState(null);
      return null;
    }
    cursor = result.data.next_cursor;
  }
  return null;
}
