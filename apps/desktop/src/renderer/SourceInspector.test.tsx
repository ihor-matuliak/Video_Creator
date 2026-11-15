import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PLAYBACK_REASONS } from '../shared/api';
import type { SourceDto } from '../shared/api';
import { SourceInspector } from './SourceInspector';
import { makeSource } from './test-bridge';

function show(source: SourceDto): void {
  render(<SourceInspector source={source} />);
}

const PROXY_BASE = { direct: false, direct_audio_stream_index: null } as const;

describe('SourceInspector: video facts (docs/03 section 5)', () => {
  it('shows the resolution, the frame rate, the constant-rate badge and the viewing way', () => {
    show(
      makeSource({
        video: { width: 1920, height: 1080, frame_rate: '30000/1001', vfr: false },
        playback: { direct: true, reason: null, direct_audio_stream_index: 1 },
      }),
    );

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^1920×1080$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^29,97 к\/с$/);
    expect(screen.getByTestId('source-vfr')).toHaveTextContent('✓ Постійна частота кадрів');
    expect(screen.getByTestId('source-playback')).toHaveTextContent(/^Напряму$/);
  });

  it('shows the whole number of frames without decimals', () => {
    show(makeSource({ video: { width: 320, height: 180, frame_rate: '30/1', vfr: false } }));

    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^30 к\/с$/);
  });

  it('shows a VFR source with a warning badge: an icon and the text', () => {
    show(makeSource({ video: { width: 320, height: 180, frame_rate: '30/1', vfr: true } }));

    const badge = screen.getByTestId('source-vfr');
    expect(badge).toHaveTextContent('⚠ Змінна частота кадрів (VFR)');
    expect(badge).toHaveClass('status-warning');
    expect(badge.querySelector('[aria-hidden="true"]')).toHaveTextContent('⚠');
  });

  it('says it is not known when the probe did not give a fact', () => {
    show(makeSource({ video: { width: null, height: null, frame_rate: null, vfr: null } }));

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-vfr')).toHaveTextContent(/^◌ VFR: невідомо$/);
    // The frame rate and the badge side by side read as two different unknowns.
    expect(screen.getByTestId('source-fps').parentElement).toHaveTextContent(
      /^невідомо ◌ VFR: невідомо$/,
    );
  });

  it('does not make up a resolution from half of it', () => {
    show(makeSource({ video: { width: 1920, height: null, frame_rate: '25/1', vfr: false } }));

    expect(screen.getByTestId('source-resolution')).toHaveTextContent(/^невідомо$/);
    expect(screen.getByTestId('source-fps')).toHaveTextContent(/^25 к\/с$/);
  });

  it('says that the video stays on this computer', () => {
    show(makeSource());

    expect(screen.getByTestId('source-local-note')).toHaveTextContent(
      "Відео залишається на цьому комп'ютері.",
    );
    expect(screen.getByTestId('source-local-note').textContent).toBe(
      "Відео залишається на цьому комп'ютері.",
    );
  });
});

describe('SourceInspector: how the source is viewed', () => {
  it.each([
    [PLAYBACK_REASONS.container, 'контейнер файлу не підтримується програвачем'],
    [PLAYBACK_REASONS.videoCodec, 'відеокодек не підтримується програвачем'],
    [PLAYBACK_REASONS.audioCodec, 'звуковий кодек першої доріжки не підтримується програвачем'],
    [PLAYBACK_REASONS.origin, 'відео починається не з нульової позначки часу'],
    [PLAYBACK_REASONS.missing, 'не вдалося визначити, чи підтримується оригінал програвачем'],
  ])('needs a copy for %s, and says why in Ukrainian', (reason, why) => {
    show(makeSource({ playback: { ...PROXY_BASE, reason } }));

    const playback = screen.getByTestId('source-playback');
    expect(playback).toHaveTextContent(`Потрібна копія для перегляду. Причина: ${why}`);
    expect(playback).not.toHaveTextContent(reason);
    expect(playback).not.toHaveTextContent('Напряму');
  });

  it('says "Невідомо" for a reason code it does not know', () => {
    show(makeSource({ playback: { ...PROXY_BASE, reason: 'SOMETHING_NEW' } }));

    const playback = screen.getByTestId('source-playback');
    expect(playback).toHaveTextContent('Потрібна копія для перегляду. Причина: Невідомо');
    expect(playback).not.toHaveTextContent('SOMETHING_NEW');
  });
});
