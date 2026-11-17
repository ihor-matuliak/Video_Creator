import { useCallback, useEffect, useRef, useState } from 'react';

import type {
  AnalysisEstimateDto,
  AnalysisProfile,
  AnalysisRunDto,
  ApiParams,
  JobDto,
  TranscriptDto,
} from '../shared/api';
import type { ApiErrorInfo, ApiResult } from '../shared/bridge';
import type { AnalysisView, ProfileDraft } from './analysis-logic';
import { DEFAULT_DRAFT, deriveView, profileFromDraft, transcriptBlockReason } from './analysis-logic';
import { callApi } from './bridge-calls';
import { useTranscriptChanged } from './transcript-events';
import { isJobOpen } from './transcription-logic';
import { useViewActive } from './view-active';

/** How often a search that is going is asked again (ADR 041 §23(5)). */
export const ANALYSIS_POLL_MS = 1000;

/** Consecutive failed polls after which the panel says it lost the search. */
const MAX_POLL_MISSES = 5;

/** Pages of `transcripts.list` read at most, the same guard as the transcript panel (ADR 039). */
const MAX_PAGES = 500;

const TOO_MANY_PAGES: ApiErrorInfo = {
  code: 'TRANSCRIPT_PAGES_EXCEEDED',
  message: 'Сервер повертає забагато частин транскрипту. Спробуйте ще раз',
  retryable: true,
};

/** What one read of the panel found: the newest transcript, the newest search of the source and its job. */
interface Snapshot {
  readonly transcript: TranscriptDto | null;
  readonly run: AnalysisRunDto | null;
  readonly job: JobDto | null;
}

/**
 * The newest revision. The revisions are listed oldest first, so it is the last item of the last
 * page (as in use-transcript). Null when the source has no transcript.
 */
async function newestTranscript(sourceId: string): Promise<ApiResult<TranscriptDto | null>> {
  let newest: TranscriptDto | null = null;
  let cursor: string | undefined;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await callApi('transcripts.list', {
      source_id: sourceId,
      ...(cursor === undefined ? {} : { cursor }),
    });
    if (!result.ok) {
      return result;
    }
    newest = result.data.items.at(-1) ?? newest;
    if (result.data.next_cursor === null) {
      return { ok: true, data: newest };
    }
    cursor = result.data.next_cursor;
  }
  return { ok: false, error: TOO_MANY_PAGES };
}

/**
 * The newest search of the source with its job. `analysis.list` is newest first, so its first item is
 * the search to show. Nothing is created here; any failed read fails the whole snapshot.
 */
async function loadSnapshot(sourceId: string): Promise<ApiResult<Snapshot>> {
  const transcript = await newestTranscript(sourceId);
  if (!transcript.ok) {
    return transcript;
  }
  const list = await callApi('analysis.list', { source_id: sourceId });
  if (!list.ok) {
    return list;
  }
  const latest = list.data.items[0];
  if (latest === undefined) {
    return { ok: true, data: { transcript: transcript.data, run: null, job: null } };
  }
  const detail = await callApi('analysis.get', { id: latest.id });
  if (!detail.ok) {
    return detail;
  }
  if (detail.data.job_id === null) {
    return { ok: true, data: { transcript: transcript.data, run: detail.data, job: null } };
  }
  const job = await callApi('jobs.get', { id: detail.data.job_id });
  if (!job.ok) {
    return job;
  }
  return { ok: true, data: { transcript: transcript.data, run: detail.data, job: job.data } };
}

/**
 * An estimate together with what it was made for. It is shown only while all of them are still the
 * ones on screen: the same draft object, the same transcript, the same view session (ADR 041 §23(4)).
 */
interface Estimated {
  readonly dto: AnalysisEstimateDto;
  readonly draft: ProfileDraft;
  readonly transcriptId: string;
  readonly profile: AnalysisProfile;
  readonly session: number;
}

/** A refusal of an action, or a read that failed. A failed read is cleared by the next read that works. */
interface Failure {
  readonly origin: 'read' | 'action';
  readonly error: ApiErrorInfo;
}

export interface AnalysisState {
  readonly draft: ProfileDraft;
  /** Replaces the draft. Any change clears the estimate: the summary is for the draft it was made for. */
  readonly setDraft: (draft: ProfileDraft) => void;
  /** The newest transcript revision of the source; null when there is none. */
  readonly transcript: TranscriptDto | null;
  /** The newest search of the source; null when there is none or the user started over from it. */
  readonly run: AnalysisRunDto | null;
  /** The job of `run`, as last read. */
  readonly job: JobDto | null;
  /** The estimate of the draft as it is now; null when there is none or it is out of date. */
  readonly estimate: AnalysisEstimateDto | null;
  /** A read or a request is on its way: the buttons that send a request stay off meanwhile. */
  readonly busy: boolean;
  /** Why the last action was refused, or why a read failed; null when nothing did. */
  readonly error: ApiErrorInfo | null;
  readonly view: AnalysisView;
}

export interface AnalysisActions {
  /** Estimates the search with the draft as it is. Writes nothing and starts nothing. */
  readonly estimate: () => void;
  /** Starts the search the estimate was made for: the same profile and the same transcript. */
  readonly create: () => void;
  /** Asks the running search to stop (`jobs.cancel`). */
  readonly cancel: () => void;
  /** Continues the stopped search; `retryUnknown` true is the user's confirmed resend of unknown windows. */
  readonly resume: (retryUnknown: boolean) => void;
  /**
   * Continues a search that stopped on its ceiling, with the given one (a decimal string, as in the
   * settings). `retryUnknown` is the user's confirmed resend of unknown windows under that ceiling.
   */
  readonly resumeWithBudget: (budgetUsd: string, retryUnknown?: boolean) => void;
  /** Shows the start for a new search in place of the stopped one. Sends nothing. */
  readonly restart: () => void;
  /** Reads everything again. */
  readonly reload: () => void;
}

export interface Analysis {
  readonly state: AnalysisState;
  readonly actions: AnalysisActions;
}

/**
 * The «Що шукати» panel of one source (ADR 041 §23(3)–(5)). It reads the newest transcript and the
 * newest search with its job, when it appears and every time the view becomes active again (the
 * settings may have changed the model or the ceiling meanwhile). It asks about a search that is
 * running once a second, and only while the view is active and the job is open. Reading sends nothing
 * paid. The caller keys the hook by source: one hook serves one source.
 */
export function useAnalysis(sourceId: string): Analysis {
  const active = useViewActive();
  const [draft, setDraftState] = useState<ProfileDraft>(DEFAULT_DRAFT);
  const [transcript, setTranscript] = useState<TranscriptDto | null>(null);
  const [run, setRun] = useState<AnalysisRunDto | null>(null);
  const [job, setJob] = useState<JobDto | null>(null);
  const [estimated, setEstimated] = useState<Estimated | null>(null);
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  // Each read asks for the next number; the read effect below does it when the number changes.
  const [readRequest, setReadRequest] = useState(1);
  // The number of the read whose answer is applied: the panel is reading while the two differ.
  const [readApplied, setReadApplied] = useState(0);
  // Counts the times the view came back on screen: an estimate made before that is out of date.
  const [shownActive, setShownActive] = useState(active);
  const [session, setSession] = useState(0);
  // A paid request on its way, read at once: a second call sends nothing.
  const inFlight = useRef(false);
  // The search the user started over from: a read does not bring it back.
  const dismissedRunId = useRef<string | null>(null);

  if (shownActive !== active) {
    setShownActive(active);
    if (active) {
      setSession((value) => value + 1);
      setReadRequest((value) => value + 1);
    }
  }

  const reading = readApplied !== readRequest;

  /** Reads the state again. The answer replaces the state only if no newer read was asked for meanwhile. */
  const read = useCallback((): void => {
    setReadRequest((value) => value + 1);
  }, []);

  // A new revision of this source's transcript (an import, or a recognition job that closed): the
  // estimate made on the old one is dropped at once, and the newest transcript is read again (ADR 041 §23(10)).
  useTranscriptChanged(sourceId, () => {
    setEstimated(null);
    read();
  });

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    // An object, not a bare boolean: the cleanup flips it while the read below awaits.
    const own = { cancelled: false };
    void loadSnapshot(sourceId).then((result) => {
      if (own.cancelled) {
        return;
      }
      setReadApplied(readRequest);
      if (!result.ok) {
        setFailure({ origin: 'read', error: result.error });
        return;
      }
      const { transcript: newest, run: latest, job: latestJob } = result.data;
      const hidden = latest === null || latest.id === dismissedRunId.current;
      setTranscript(newest);
      setRun(hidden ? null : latest);
      setJob(hidden ? null : latestJob);
      setFailure((current) => (current?.origin === 'read' ? null : current));
    });
    return () => {
      own.cancelled = true;
    };
  }, [active, sourceId, readRequest]);

  const busy = reading || sending;
  const view = deriveView(run, job);
  const shownEstimate: Estimated | null =
    estimated !== null &&
    estimated.draft === draft &&
    estimated.session === session &&
    estimated.transcriptId === transcript?.id
      ? estimated
      : null;

  // The search is watched while the view is on screen and its job is open. A read that is applied
  // starts the chain again, so a poll that gave up can go on after the next read.
  const openJob = active && job !== null && isJobOpen(job.state) ? job : null;
  const watchedRunId = run !== null && openJob !== null ? run.id : null;
  const watchedJobId = openJob?.id ?? null;

  useEffect(() => {
    if (watchedRunId === null || watchedJobId === null) {
      return undefined;
    }
    const runId = watchedRunId;
    const jobId = watchedJobId;
    // A call, not the property: TypeScript keeps a property narrowed to false across an await.
    const watch = { stopped: false };
    const stopped = (): boolean => watch.stopped;
    let timer: number | undefined;

    function schedule(misses: number): void {
      timer = window.setTimeout(() => {
        void poll(misses);
      }, ANALYSIS_POLL_MS);
    }

    /** A read that failed counts as a miss: after MAX_POLL_MISSES in a row the panel says it lost the search. */
    function miss(misses: number, error: ApiErrorInfo): void {
      if (misses + 1 < MAX_POLL_MISSES) {
        schedule(misses + 1);
      } else {
        setFailure({ origin: 'read', error });
      }
    }

    async function poll(misses: number): Promise<void> {
      const runRead = await callApi('analysis.get', { id: runId });
      if (stopped()) {
        return;
      }
      if (!runRead.ok) {
        miss(misses, runRead.error);
        return;
      }
      const jobRead = await callApi('jobs.get', { id: jobId });
      if (stopped()) {
        return;
      }
      if (!jobRead.ok) {
        miss(misses, jobRead.error);
        return;
      }
      if (!isJobOpen(jobRead.data.state)) {
        // The job is over. The backend writes the run as completed before it closes the job, so the run
        // is read once more and set together with the job: the closed job is never shown next to the run
        // as the previous poll left it. A failed read is a miss like any other. No poll follows; the read
        // below replaces what is shown, and the job no longer counts as open.
        const finalRead = await callApi('analysis.get', { id: runId });
        if (stopped()) {
          return;
        }
        if (!finalRead.ok) {
          miss(misses, finalRead.error);
          return;
        }
        if (finalRead.data.id !== dismissedRunId.current) {
          setRun(finalRead.data);
        }
        setJob(jobRead.data);
        read();
        return;
      }
      if (runRead.data.id === dismissedRunId.current) {
        return;
      }
      setRun(runRead.data);
      setJob(jobRead.data);
      schedule(0);
    }

    schedule(0);
    return () => {
      watch.stopped = true;
      window.clearTimeout(timer);
    };
  }, [watchedRunId, watchedJobId, readApplied, read]);

  /**
   * Sends one request. Nothing is sent while another paid one is on its way or the panel is reading.
   * The answer is used as the request was made for it; a refusal is shown, and the two codes that mean
   * the panel is out of date are read again (ADR 041 §23(4), (5)). Nothing is retried by itself.
   */
  const send = <T>(call: () => Promise<ApiResult<T>>, accepted: (data: T) => void): void => {
    if (busy || inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSending(true);
    setFailure(null);
    void call().then((result) => {
      inFlight.current = false;
      setSending(false);
      if (result.ok) {
        accepted(result.data);
        return;
      }
      setFailure({ origin: 'action', error: result.error });
      if (result.error.code === 'TRANSCRIPT_REVISION_STALE') {
        setEstimated(null);
        read();
      } else if (result.error.code === 'ANALYSIS_RUN_ACTIVE') {
        read();
      }
    });
  };

  const estimate = (): void => {
    const profile = profileFromDraft(draft);
    if (
      profile === null ||
      transcript === null ||
      transcriptBlockReason(transcript, draft.allowPartial) !== null
    ) {
      return;
    }
    const sentDraft = draft;
    const transcriptId = transcript.id;
    send(
      () => callApi('analysis.estimate', { source_id: sourceId, transcript_id: transcriptId, profile }),
      (dto) => {
        setEstimated({ dto, draft: sentDraft, transcriptId, profile, session });
      },
    );
  };

  const create = (): void => {
    if (shownEstimate === null) {
      return;
    }
    const { transcriptId, profile } = shownEstimate;
    send(
      () => callApi('analysis.create', { source_id: sourceId, transcript_id: transcriptId, profile }),
      () => {
        setEstimated(null);
        read();
      },
    );
  };

  const cancel = (): void => {
    const jobId = run?.job_id ?? null;
    if (view.kind !== 'running' || jobId === null) {
      return;
    }
    send(
      () => callApi('jobs.cancel', { id: jobId }),
      () => {
        read();
      },
    );
  };

  /** Sends the resume of the stopped search's job; `extra` holds only what this resume changes. */
  const resumeJob = (extra: Omit<ApiParams['jobs.resume'], 'id'>): void => {
    const jobId = run?.job_id ?? null;
    if (view.kind !== 'stopped' || jobId === null) {
      return;
    }
    send(
      () => callApi('jobs.resume', { id: jobId, ...extra }),
      () => {
        read();
      },
    );
  };

  const resume = (retryUnknown: boolean): void => {
    resumeJob(retryUnknown ? { retry_unknown: true } : {});
  };

  const resumeWithBudget = (budgetUsd: string, retryUnknown = false): void => {
    resumeJob(
      retryUnknown ? { budget_usd: budgetUsd, retry_unknown: true } : { budget_usd: budgetUsd },
    );
  };

  const restart = (): void => {
    if (busy || view.kind === 'running') {
      return;
    }
    // Only a search on screen is dismissed: a restart with none keeps an earlier dismissal in force.
    if (run !== null) {
      dismissedRunId.current = run.id;
    }
    setRun(null);
    setJob(null);
    setEstimated(null);
    setFailure(null);
  };

  return {
    state: {
      draft,
      setDraft: (next: ProfileDraft): void => {
        setDraftState(next);
        setEstimated(null);
      },
      transcript,
      run,
      job,
      estimate: shownEstimate?.dto ?? null,
      busy,
      error: failure?.error ?? null,
      view,
    },
    actions: { estimate, create, cancel, resume, resumeWithBudget, restart, reload: read },
  };
}
