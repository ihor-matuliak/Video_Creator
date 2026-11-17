import { useState } from 'react';

import type { ApiErrorInfo } from '../shared/bridge';
import { importTranscript } from './bridge-calls';
import { notifyTranscriptChanged } from './transcript-events';
import { StatusText } from './ui';

type Outcome =
  | { readonly kind: 'imported'; readonly revision: number; readonly segments: number }
  | { readonly kind: 'failed'; readonly error: ApiErrorInfo };

/** The line about the new revision (ADR 039). */
function importedText(revision: number, segments: number): string {
  return `Субтитри імпортовано: ревізія ${String(revision)}, сегментів ${String(segments)}.`;
}

/**
 * «Імпортувати субтитри» under the recognition block (ADR 039): an .srt or .vtt file becomes a new
 * revision of the source's transcript. It needs neither a key nor an available file, so it is always
 * offered. A cancelled dialog imports nothing. Starting an import clears the line about the one before,
 * so that line never shows while a new import runs. The parent keys it by the source, so a result never
 * outlives the source it was about.
 */
export function TranscriptImport({ sourceId }: { readonly sourceId: string }) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const importFile = (): void => {
    if (busy) {
      return;
    }
    setOutcome(null);
    setBusy(true);
    void importTranscript(sourceId).then((result) => {
      setBusy(false);
      if ('canceled' in result) {
        return;
      }
      if (result.ok) {
        // A new revision exists now: the panels that read this source's transcript are told (ADR 041 §23(10)).
        notifyTranscriptChanged(sourceId);
      }
      setOutcome(
        result.ok
          ? { kind: 'imported', revision: result.data.revision, segments: result.data.segment_count }
          : { kind: 'failed', error: result.error },
      );
    });
  };

  return (
    <div data-testid="transcript-import">
      <div className="actions">
        <button
          type="button"
          className="button"
          data-testid="transcript-import-button"
          disabled={busy}
          onClick={() => {
            importFile();
          }}
        >
          Імпортувати субтитри (SRT, VTT)
        </button>
      </div>
      {outcome?.kind === 'imported' && (
        <p role="status" data-testid="transcript-import-result">
          <StatusText tone="success">{importedText(outcome.revision, outcome.segments)}</StatusText>
        </p>
      )}
      {outcome?.kind === 'failed' && (
        <p role="alert" data-testid="transcript-import-error" className="field-error">
          <span aria-hidden="true">✕</span> {outcome.error.message}
        </p>
      )}
    </div>
  );
}
