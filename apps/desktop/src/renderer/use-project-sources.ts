import { useCallback, useEffect, useState } from 'react';

import type { SourceDto } from '../shared/api';
import type { ApiErrorInfo, ApiResult } from '../shared/bridge';
import { callApi } from './bridge-calls';

/** A guard against a backend that keeps returning a cursor. */
const MAX_PAGES = 100;

/**
 * Every source of a project: follows `next_cursor` page by page and merges the pages in the
 * order the backend gave them. A source that turns up twice (the list moved while it was
 * read) is kept once, in its latest form. The first failed page fails the whole load, so a
 * half-read list is never shown as if it were complete.
 */
export async function loadAllSources(projectId: string): Promise<ApiResult<readonly SourceDto[]>> {
  const merged = new Map<string, SourceDto>();
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('sources.list', {
      project_id: projectId,
      ...(cursor === undefined ? {} : { cursor }),
    });
    if (!result.ok) {
      return result;
    }
    for (const source of result.data.items) {
      merged.set(source.id, source);
    }
    if (result.data.next_cursor === null) {
      break;
    }
    cursor = result.data.next_cursor;
  }
  return { ok: true, data: [...merged.values()] };
}

export type SourcesLoad =
  | { readonly kind: 'loading' }
  | { readonly kind: 'error'; readonly error: ApiErrorInfo }
  | { readonly kind: 'ready' };

export interface ProjectSources {
  readonly load: SourcesLoad;
  readonly sources: readonly SourceDto[];
  /** A source registered now: appended, or replaced when it is already known. */
  readonly addSource: (added: SourceDto) => void;
  /** A fresher form of a known source (its hash finished); unknown ones are ignored. */
  readonly refreshSource: (fresh: SourceDto) => void;
  /** Reads the list again after a failure. */
  readonly reload: () => void;
}

/** The sources of one project, read from the backend when the project is opened. */
export function useProjectSources(projectId: string): ProjectSources {
  const [load, setLoad] = useState<SourcesLoad>({ kind: 'loading' });
  const [sources, setSources] = useState<readonly SourceDto[]>([]);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // An object, not a bare boolean: the cleanup flips it while the load below awaits.
    const run = { cancelled: false };
    void loadAllSources(projectId).then((result) => {
      if (run.cancelled) {
        return;
      }
      if (result.ok) {
        setSources(result.data);
        setLoad({ kind: 'ready' });
      } else {
        setLoad({ kind: 'error', error: result.error });
      }
    });
    return () => {
      run.cancelled = true;
    };
  }, [projectId, attempt]);

  const reload = useCallback(() => {
    setLoad({ kind: 'loading' });
    setAttempt((value) => value + 1);
  }, []);

  const addSource = useCallback((added: SourceDto) => {
    setSources((known) =>
      known.some((source) => source.id === added.id)
        ? known.map((source) => (source.id === added.id ? added : source))
        : [...known, added],
    );
  }, []);

  const refreshSource = useCallback((fresh: SourceDto) => {
    setSources((known) => known.map((source) => (source.id === fresh.id ? fresh : source)));
  }, []);

  return { load, sources, addSource, refreshSource, reload };
}
