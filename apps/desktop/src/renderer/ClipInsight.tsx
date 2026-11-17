import type { ClipDto } from '../shared/api';

export interface ClipInsightProps {
  readonly clip: ClipDto;
}

/**
 * Why an AI clip was chosen and its priority score, shown above the editor (ADR 041 §23(7),
 * docs/03 §8). A manual clip has neither, so nothing is rendered for it. The reason is plain
 * text: the model's words are never read as markup.
 */
export function ClipInsight({ clip }: ClipInsightProps) {
  if (clip.analysis_run_id === null) {
    return null;
  }
  const reason = clip.reason !== null && clip.reason.trim() !== '' ? clip.reason : null;
  if (reason === null && clip.rank === null) {
    return null;
  }
  return (
    <section className="panel clip-insight" aria-label="Оцінка фрагмента" data-testid="clip-insight">
      {reason !== null && (
        <div className="clip-insight-reason">
          <h4>Чому обрано</h4>
          <p data-testid="clip-insight-reason">{reason}</p>
        </div>
      )}
      {clip.rank !== null && (
        <div className="clip-insight-score">
          <p data-testid="clip-insight-rank">{`Оцінка пріоритету: ${String(clip.rank)} зі 100`}</p>
          <p className="note">Це не прогноз переглядів</p>
        </div>
      )}
    </section>
  );
}
