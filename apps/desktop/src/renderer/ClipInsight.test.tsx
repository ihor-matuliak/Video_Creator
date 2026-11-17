import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ClipDto } from '../shared/api';
import { ClipInsight } from './ClipInsight';
import { ClipList } from './ClipList';
import { makeClip } from './test-bridge';

const RUN_ID = '3f5a7c9e-1b2d-4e6f-8a0b-2c4d6e8f0a1b';
const REASON = 'Кіт зіскакує зі стільця і падає в миску з водою.';

/** A clip found by a run: categories, reason, score and both warnings. */
const AI_CLIP = makeClip({
  analysis_run_id: RUN_ID,
  categories: ['funny', 'unexpected'],
  reason: REASON,
  rank: 85,
  visual_review_required: true,
  analysis_stale: true,
});

/** A manual clip. The AI fields are set on it as well: only a null run id decides that it is manual. */
const MANUAL_CLIP = makeClip({
  categories: ['funny'],
  reason: REASON,
  rank: 85,
  visual_review_required: true,
  analysis_stale: true,
});

function renderCards(clips: readonly ClipDto[]) {
  return render(
    <ClipList
      clips={clips}
      selected={new Set()}
      busy={new Set()}
      errors={{}}
      onActivate={() => undefined}
      onToggleSelected={() => undefined}
      onReview={() => undefined}
    />,
  );
}

describe('ClipList card of an AI clip', () => {
  it('shows the category chips, the priority and the two warnings', () => {
    renderCards([AI_CLIP]);

    const card = screen.getByTestId('clip-card');
    expect(within(card).getByText('Смішні моменти')).toBeInTheDocument();
    expect(within(card).getByText('Несподівані ситуації')).toBeInTheDocument();
    expect(within(card).getByText('Пріоритет: високий')).toBeInTheDocument();
    expect(within(card).getByText('Перевірити картинку')).toBeInTheDocument();
    expect(within(card).getByText('Оцінка застаріла')).toBeInTheDocument();
  });
});

describe('ClipList card of a manual clip', () => {
  it('shows none of the AI texts', () => {
    renderCards([MANUAL_CLIP]);

    const card = screen.getByTestId('clip-card');
    expect(within(card).queryByTestId('clip-ai')).toBeNull();
    expect(within(card).queryByText('Смішні моменти')).not.toBeInTheDocument();
    expect(within(card).queryByText('Пріоритет: високий')).not.toBeInTheDocument();
    expect(within(card).queryByText('Перевірити картинку')).not.toBeInTheDocument();
    expect(within(card).queryByText('Оцінка застаріла')).not.toBeInTheDocument();
  });
});

describe('ClipInsight', () => {
  it('shows the reason under «Чому обрано» and the note that it is not a view forecast', () => {
    render(<ClipInsight clip={AI_CLIP} />);

    expect(screen.getByText('Чому обрано')).toBeInTheDocument();
    expect(screen.getByText(REASON)).toBeInTheDocument();
    expect(screen.getByText('Це не прогноз переглядів')).toBeInTheDocument();
  });

  it('shows the score of rank 85 as «Оцінка пріоритету: 85 зі 100»', () => {
    render(<ClipInsight clip={AI_CLIP} />);

    expect(screen.getByText('Оцінка пріоритету: 85 зі 100')).toBeInTheDocument();
  });

  it('shows a reason that contains markup as literal text', () => {
    const { container } = render(
      <ClipInsight clip={makeClip({ analysis_run_id: RUN_ID, reason: '<b>x</b>' })} />,
    );

    expect(screen.getByText('<b>x</b>')).toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
  });

  it('renders nothing for a manual clip', () => {
    const { container } = render(<ClipInsight clip={MANUAL_CLIP} />);

    expect(container).toBeEmptyDOMElement();
  });
});
