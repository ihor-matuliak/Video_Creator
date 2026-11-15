import type { AudioStreamDto, SourceDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import { NOT_KNOWN, describePlayback, formatFrameRate, formatResolution, vfrBadge } from './source-format';
import { StatusText } from './ui';
import type { Tone } from './ui';

interface Label {
  readonly tone: Tone;
  readonly text: string;
}

const AVAILABILITY: Readonly<Record<string, Label>> = {
  available: { tone: 'success', text: 'Доступний' },
  missing: { tone: 'danger', text: 'Файл не знайдено на цьому місці' },
  changed: { tone: 'warning', text: 'Файл змінено після додавання' },
};

const HASH_STATUS: Readonly<Record<string, Label>> = {
  pending: { tone: 'neutral', text: 'Перевіряється у фоні' },
  computed: { tone: 'success', text: 'Підтверджена' },
  failed: { tone: 'danger', text: 'Не вдалося перевірити' },
};

const UNKNOWN: Label = { tone: 'warning', text: 'Невідомо' };

function labelFor(table: Readonly<Record<string, Label>>, value: string): Label {
  return Object.hasOwn(table, value) ? (table[value] ?? UNKNOWN) : UNKNOWN;
}

function describeTrack(stream: AudioStreamDto): string {
  const parts = [
    stream.language,
    stream.title,
    stream.channels === null ? null : `${String(stream.channels)} кан.`,
    stream.codec === '' ? null : stream.codec.toUpperCase(),
    stream.default ? 'за замовчуванням' : null,
  ].filter((part): part is string => part !== null && part !== '');
  return parts.length === 0 ? `Доріжка ${String(stream.index)}` : `Доріжка ${String(stream.index)} · ${parts.join(' · ')}`;
}

/** What the user needs to know about one registered source (docs/03 section 5). */
export function SourceInspector({ source }: { readonly source: SourceDto }) {
  const availability = labelFor(AVAILABILITY, source.availability);
  const hash = labelFor(HASH_STATUS, source.hash_status);
  const vfr = vfrBadge(source.video.vfr);

  return (
    <section className="panel inspector" data-testid="source-ready" aria-labelledby="source-title">
      <h3 id="source-title">{source.display_name}</h3>
      <dl className="facts">
        <div>
          <dt>Тривалість</dt>
          <dd data-testid="source-duration">{formatTimecode(source.duration_ms)}</dd>
        </div>
        <div>
          <dt>Роздільна здатність</dt>
          <dd data-testid="source-resolution">{formatResolution(source.video) ?? NOT_KNOWN}</dd>
        </div>
        <div>
          <dt>Частота кадрів</dt>
          <dd>
            <span data-testid="source-fps">{formatFrameRate(source.video.frame_rate) ?? NOT_KNOWN}</span>{' '}
            <StatusText tone={vfr.tone} testId="source-vfr">
              {vfr.text}
            </StatusText>
          </dd>
        </div>
        <div>
          <dt>Перегляд</dt>
          <dd data-testid="source-playback">{describePlayback(source.playback)}</dd>
        </div>
        <div>
          <dt>Файл</dt>
          <dd>
            <StatusText tone={availability.tone} testId="source-availability">
              {availability.text}
            </StatusText>
          </dd>
        </div>
        <div>
          <dt>Ідентичність файлу</dt>
          <dd>
            <StatusText tone={hash.tone} testId="source-hash-status">
              {hash.text}
            </StatusText>
          </dd>
        </div>
        <div>
          <dt>Звукові доріжки</dt>
          <dd data-testid="source-audio">
            {source.audio_streams.length === 0 ? (
              <StatusText tone="warning">
                У відео немає звукової доріжки. Можна створювати фрагменти вручну
              </StatusText>
            ) : (
              <ul className="plain-list">
                {source.audio_streams.map((stream) => (
                  <li key={stream.index}>{describeTrack(stream)}</li>
                ))}
              </ul>
            )}
          </dd>
        </div>
      </dl>
      <p className="note" data-testid="source-local-note">
        Відео залишається на цьому комп&apos;ютері.
      </p>
    </section>
  );
}
