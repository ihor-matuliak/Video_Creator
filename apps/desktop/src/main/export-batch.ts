import { isUuid4 } from '../shared/api';
import type { ExportProfile } from '../shared/api';
import { parseProfile } from './api-ops';

/**
 * Reads of a backend export batch that only main makes: `exports.retry` needs the batch's
 * own `output_dir` and profile, and `export:open-folder` its `output_dir`. None of them is
 * ever given to the renderer. Every reader throws a `TypeError` that names no value when the
 * answer is malformed; the gateway turns that into BAD_BACKEND_RESPONSE.
 */

/** Batch statuses of a batch that is still being worked on (docs/04 section 5). */
const OPEN_STATUSES: readonly string[] = ['queued', 'running'];

/** Item states that a retry exports again. */
const RETRY_STATES: readonly string[] = ['failed', 'cancelled'];

export interface RetrySource {
  readonly status: string;
  readonly project_id: string;
  readonly output_dir: string;
  readonly profile: ExportProfile;
  /** Clips of the failed and cancelled items: first-seen order, each once. */
  readonly clip_ids: readonly string[];
}

function bad(): never {
  throw new TypeError('Некоректна відповідь локального рушія');
}

function fields(value: unknown): Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : bad();
}

function text(value: unknown): string {
  return typeof value === 'string' && value !== '' ? value : bad();
}

export function isOpenBatchStatus(status: string): boolean {
  return OPEN_STATUSES.includes(status);
}

/** `output_dir` of a backend batch answer. */
export function parseOutputDir(value: unknown): string {
  return text(fields(value).output_dir);
}

export function parseRetrySource(value: unknown): RetrySource {
  const batch = fields(value);
  const items = batch.items;
  if (!Array.isArray(items)) {
    return bad();
  }
  const clipIds: string[] = [];
  for (const raw of items as unknown[]) {
    const item = fields(raw);
    const state = text(item.state);
    const clipId = item.clip_id;
    if (!isUuid4(clipId)) {
      return bad();
    }
    if (RETRY_STATES.includes(state) && !clipIds.includes(clipId)) {
      clipIds.push(clipId);
    }
  }
  let profile: ExportProfile;
  try {
    profile = parseProfile(batch.profile);
  } catch {
    return bad();
  }
  const projectId = batch.project_id;
  if (!isUuid4(projectId)) {
    return bad();
  }
  return {
    status: text(batch.status),
    project_id: projectId,
    output_dir: text(batch.output_dir),
    profile,
    clip_ids: clipIds,
  };
}
