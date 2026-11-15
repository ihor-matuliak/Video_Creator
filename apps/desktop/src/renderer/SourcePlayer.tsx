import { useEffect, useId, useImperativeHandle, useRef, useState } from 'react';
import type { ChangeEvent, Ref, SyntheticEvent } from 'react';

import type { AudioStreamDto, SourceDto } from '../shared/api';
import { formatTimecode } from '../shared/timecode';
import {
  PLAYBACK_SPEEDS,
  canonicalMs,
  defaultAudioIndex,
  mediaUrl,
  playsDirectly,
  proxyReasonText,
} from './player-logic';
import { HOTKEY_LABELS } from './hotkey-logic';
import { StatusText } from './ui';
import { usePreviewProxy } from './use-preview-proxy';

/** What the clip forms and the keyboard shortcuts may ask of the player. */
export interface PlayerHandle {
  /** The canonical millisecond of the playhead; null while no video is loaded. */
  readonly currentMs: () => number | null;
  /**
   * Plays [startMs, endMs). At the end it pauses, or, while «Повтор фрагмента» is on, goes back to
   * the start and plays on. Whether the pass loops is read at its end, so the button can be
   * switched during a pass.
   */
  readonly playRange: (startMs: number, endMs: number) => void;
  /** Plays or pauses; nothing while no video is loaded. */
  readonly togglePlay: () => void;
  /**
   * Moves the playhead (kept inside the source) and drops a fragment that was being played.
   * Before the video has loaded the position is applied as soon as it has.
   */
  readonly seekTo: (ms: number) => void;
  /** Moves the playhead by `deltaMs`; nothing while no video is loaded. */
  readonly seekBy: (deltaMs: number) => void;
}

export interface SourcePlayerProps {
  readonly source: SourceDto;
  readonly ref?: Ref<PlayerHandle>;
  /** Told whenever a video has (or no longer has) loaded enough to know its position. */
  readonly onReadyChange?: (ready: boolean) => void;
}

/** MediaError.MEDIA_ERR_ABORTED: the load was replaced on purpose, not a failure. */
const MEDIA_ERR_ABORTED = 1;
/** How often a playing fragment is checked against its end (timeupdate alone is ~4 Hz). */
const RANGE_CHECK_MS = 40;

/** The fragment that is being played: [startMs, endMs). */
interface PlayedRange {
  readonly startMs: number;
  readonly endMs: number;
}

/** Back to the start of a fragment, playing on (also from the end of the media, where it has ended). */
function restartAt(video: HTMLVideoElement, startMs: number): Promise<void> {
  video.currentTime = startMs / 1000;
  return video.play();
}

function toggleVideo(video: HTMLVideoElement): void {
  if (video.paused) {
    void video.play().catch(() => undefined);
  } else {
    video.pause();
  }
}

function describeTrack(stream: AudioStreamDto): string {
  const parts = [stream.language, stream.title].filter(
    (part): part is string => part !== null && part !== '',
  );
  return parts.length === 0
    ? `Доріжка ${String(stream.index)}`
    : `Доріжка ${String(stream.index)} · ${parts.join(' · ')}`;
}

export function SourcePlayer({ source, ref, onReadyChange }: SourcePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  /** Where to continue after the source of the video was swapped (audio track change). */
  const resumeAtRef = useRef<number | null>(null);
  const audioId = useId();
  const speedId = useId();

  const [audioIndex, setAudioIndex] = useState<number | null>(() => defaultAudioIndex(source));
  const [directFailed, setDirectFailed] = useState(false);
  const [proxyBroken, setProxyBroken] = useState(false);
  const [readySrc, setReadySrc] = useState<string | null>(null);
  const [positionMs, setPositionMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(1);
  const [rate, setRate] = useState(1);
  const [range, setRange] = useState<PlayedRange | null>(null);
  const [loop, setLoop] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  // The picture's own proportions once the video has told them; 16:9 until then (styles.css).
  const [aspect, setAspect] = useState<string | null>(null);

  const durationMs = source.duration_ms;
  const direct = playsDirectly(source, audioIndex) && !directFailed;
  const proxy = usePreviewProxy(source.id, audioIndex, !direct);

  let src: string | undefined;
  if (direct) {
    src = mediaUrl('source', source.id);
  } else if (proxy.state.kind === 'ready' && !proxyBroken) {
    src = mediaUrl('preview', proxy.state.previewId);
  }
  const ready = src !== undefined && readySrc === src;

  useEffect(() => {
    onReadyChange?.(ready);
  }, [ready, onReadyChange]);

  useEffect(() => {
    const video = videoRef.current;
    if (video !== null) {
      video.volume = volume;
      video.playbackRate = rate;
    }
  }, [volume, rate, src]);

  useEffect(() => {
    const onChange = (): void => {
      const stage = stageRef.current;
      setFullscreen(stage !== null && document.fullscreenElement === stage);
    };
    document.addEventListener('fullscreenchange', onChange);
    return () => {
      document.removeEventListener('fullscreenchange', onChange);
    };
  }, []);

  // A fragment that is being played pauses at its end, or starts over while the loop is on:
  // timeupdate alone arrives about four times a second, too coarse for "stop after end".
  // The loop is a dependency, so switching it makes the very next check use the new value.
  useEffect(() => {
    if (range === null) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      const video = videoRef.current;
      if (video === null || video.currentTime * 1000 < range.endMs) {
        return;
      }
      if (loop) {
        setPositionMs(range.startMs);
        restartAt(video, range.startMs).catch(() => {
          setRange(null);
        });
      } else {
        video.pause();
        video.currentTime = range.endMs / 1000;
        setPositionMs(range.endMs);
        setRange(null);
      }
    }, RANGE_CHECK_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [range, loop]);

  useImperativeHandle(
    ref,
    () => ({
      currentMs: () => {
        const video = videoRef.current;
        return video === null || !ready ? null : canonicalMs(video.currentTime);
      },
      playRange: (startMs, endMs) => {
        const video = videoRef.current;
        if (video === null || !ready) {
          return;
        }
        video.currentTime = startMs / 1000;
        setPositionMs(startMs);
        setRange({ startMs, endMs });
        void video.play().catch(() => {
          setRange(null);
        });
      },
      togglePlay: () => {
        const video = videoRef.current;
        if (video !== null && ready) {
          toggleVideo(video);
        }
      },
      seekTo: (ms) => {
        const target = Math.min(durationMs, Math.max(0, Math.round(ms)));
        setRange(null);
        setPositionMs(target);
        const video = videoRef.current;
        if (video !== null && ready) {
          video.currentTime = target / 1000;
        } else {
          resumeAtRef.current = target;
        }
      },
      seekBy: (deltaMs) => {
        const video = videoRef.current;
        if (video === null || !ready) {
          return;
        }
        const target = Math.min(durationMs, Math.max(0, canonicalMs(video.currentTime) + deltaMs));
        setRange(null);
        setPositionMs(target);
        video.currentTime = target / 1000;
      },
    }),
    [ready, durationMs],
  );

  const handleError = (event: SyntheticEvent<HTMLVideoElement>): void => {
    if (event.currentTarget.error?.code === MEDIA_ERR_ABORTED) {
      return;
    }
    resumeAtRef.current = positionMs;
    if (direct) {
      // ADR 021: a direct player that fails is replaced by the proxy of the same stream.
      setDirectFailed(true);
    } else {
      setProxyBroken(true);
    }
  };

  const handleLoadedMetadata = (event: SyntheticEvent<HTMLVideoElement>): void => {
    const video = event.currentTarget;
    video.volume = volume;
    video.playbackRate = rate;
    if (video.videoWidth > 0 && video.videoHeight > 0) {
      setAspect(`${String(video.videoWidth)} / ${String(video.videoHeight)}`);
    }
    if (resumeAtRef.current !== null) {
      video.currentTime = resumeAtRef.current / 1000;
      setPositionMs(resumeAtRef.current);
      resumeAtRef.current = null;
    }
    setReadySrc(src ?? null);
  };

  const handleTimeUpdate = (event: SyntheticEvent<HTMLVideoElement>): void => {
    setPositionMs(canonicalMs(event.currentTarget.currentTime));
  };

  const togglePlay = (): void => {
    const video = videoRef.current;
    if (video !== null) {
      toggleVideo(video);
    }
  };

  const seekTo = (event: ChangeEvent<HTMLInputElement>): void => {
    const video = videoRef.current;
    const next = Number(event.target.value);
    if (video !== null && Number.isFinite(next)) {
      // The user took the playhead: the fragment is no longer being played.
      setRange(null);
      video.currentTime = next / 1000;
      setPositionMs(next);
    }
  };

  const changeAudio = (event: ChangeEvent<HTMLSelectElement>): void => {
    const next = Number(event.target.value);
    if (!Number.isInteger(next) || next === audioIndex) {
      return;
    }
    const video = videoRef.current;
    resumeAtRef.current = video === null ? positionMs : canonicalMs(video.currentTime);
    setProxyBroken(false);
    setAudioIndex(next);
  };

  const toggleFullscreen = (): void => {
    const stage = stageRef.current;
    if (stage === null) {
      return;
    }
    if (document.fullscreenElement === stage) {
      void document.exitFullscreen().catch(() => undefined);
    } else {
      void stage.requestFullscreen().catch(() => undefined);
    }
  };

  let failedMessage: string | null = null;
  if (!direct) {
    if (proxy.state.kind === 'failed') {
      failedMessage = proxy.state.message;
    } else if (proxyBroken) {
      failedMessage = 'Не вдалося відтворити версію для перегляду';
    }
  }

  return (
    <div
      className="player"
      data-testid="player"
      data-mode={direct ? 'direct' : 'proxy'}
      ref={stageRef}
    >
      <div
        className="player-stage"
        data-testid="player-stage"
        style={aspect === null ? undefined : { aspectRatio: aspect }}
      >
        {src !== undefined ? (
          <video
            key={src}
            ref={videoRef}
            className="player-video"
            data-testid="player-video"
            aria-label={`Відео ${source.display_name}`}
            src={src}
            preload="metadata"
            playsInline
            onError={handleError}
            onLoadedMetadata={handleLoadedMetadata}
            onTimeUpdate={handleTimeUpdate}
            onPlay={() => {
              setPlaying(true);
            }}
            onPause={() => {
              setPlaying(false);
            }}
            onEnded={(event) => {
              // A fragment that ends where the source ends never reaches the check of the end.
              if (range !== null && loop) {
                setPositionMs(range.startMs);
                restartAt(event.currentTarget, range.startMs).catch(() => {
                  setPlaying(false);
                  setRange(null);
                });
                return;
              }
              setPlaying(false);
              setRange(null);
            }}
          />
        ) : (
          <div className="player-placeholder">
            {proxy.state.kind === 'preparing' && (
              <div role="status" data-testid="proxy-progress" className="proxy-progress">
                <p>
                  {proxy.state.cancelling
                    ? 'Скасовуємо підготовку версії для перегляду…'
                    : 'Готуємо версію для перегляду…'}
                  {proxy.state.percent === null ? '' : ` ${String(proxy.state.percent)}%`}
                </p>
                <progress
                  max={100}
                  value={proxy.state.percent ?? undefined}
                  aria-label="Підготовка версії для перегляду"
                />
                <button
                  type="button"
                  className="button"
                  data-testid="proxy-cancel"
                  disabled={proxy.state.jobId === null || proxy.state.cancelling}
                  onClick={proxy.cancel}
                >
                  Скасувати
                </button>
              </div>
            )}
            {proxy.state.kind === 'cancelled' && (
              <div role="status" data-testid="proxy-cancelled" className="proxy-progress">
                <p>
                  <StatusText tone="neutral">Підготовку версії для перегляду скасовано.</StatusText>
                </p>
                <button type="button" className="button" data-testid="proxy-retry" onClick={proxy.retry}>
                  Спробувати ще раз
                </button>
              </div>
            )}
            {failedMessage !== null && (
              <div role="alert" data-testid="proxy-error" className="proxy-progress">
                <p>
                  <StatusText tone="danger">
                    Не вдалося підготувати версію для перегляду. {failedMessage}
                  </StatusText>
                </p>
                <button
                  type="button"
                  className="button"
                  data-testid="proxy-retry"
                  onClick={() => {
                    setProxyBroken(false);
                    proxy.retry();
                  }}
                >
                  Спробувати ще раз
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {!direct && (
        <p className="note" data-testid="proxy-reason">
          <span aria-hidden="true">ⓘ </span>
          {proxyReasonText(source, audioIndex, directFailed)}
        </p>
      )}

      <div className="player-controls">
        <button
          type="button"
          className="button"
          data-testid="player-play"
          aria-keyshortcuts={HOTKEY_LABELS.play.aria}
          title={`${playing ? 'Пауза' : 'Грати'} (${HOTKEY_LABELS.play.title})`}
          disabled={!ready}
          onClick={togglePlay}
        >
          {playing ? 'Пауза' : 'Грати'}
        </button>
        <span className="player-time" data-testid="player-time">
          {formatTimecode(positionMs)} / {formatTimecode(source.duration_ms)}
        </span>
        <input
          type="range"
          className="player-seek"
          data-testid="player-seek"
          aria-label="Позиція у відео"
          aria-keyshortcuts={`${HOTKEY_LABELS.back.aria} ${HOTKEY_LABELS.forward.aria}`}
          title={`Позиція у відео. ${HOTKEY_LABELS.back.title}: −1 с, ${HOTKEY_LABELS.forward.title}: +1 с`}
          aria-valuetext={formatTimecode(positionMs)}
          min={0}
          max={source.duration_ms}
          step={1}
          value={Math.min(positionMs, source.duration_ms)}
          disabled={!ready}
          onChange={seekTo}
        />
      </div>

      <div className="player-controls">
        <label className="inline-field">
          <span>Гучність</span>
          <input
            type="range"
            data-testid="player-volume"
            min={0}
            max={1}
            step={0.05}
            value={volume}
            onChange={(event) => {
              setVolume(Number(event.target.value));
            }}
          />
        </label>
        <label className="inline-field" htmlFor={speedId}>
          <span>Швидкість</span>
        </label>
        <select
          id={speedId}
          className="field player-select"
          data-testid="player-speed"
          value={rate}
          onChange={(event) => {
            setRate(Number(event.target.value));
          }}
        >
          {PLAYBACK_SPEEDS.map((speed) => (
            <option key={speed} value={speed}>
              {`${String(speed)}×`}
            </option>
          ))}
        </select>
        {source.audio_streams.length > 1 && (
          <>
            <label className="inline-field" htmlFor={audioId}>
              <span>Звукова доріжка</span>
            </label>
            <select
              id={audioId}
              className="field player-select"
              data-testid="player-audio"
              value={audioIndex ?? ''}
              onChange={changeAudio}
            >
              {source.audio_streams.map((stream) => (
                <option key={stream.index} value={stream.index}>
                  {describeTrack(stream)}
                </option>
              ))}
            </select>
          </>
        )}
        <button
          type="button"
          className="button"
          data-testid="player-loop"
          aria-pressed={loop}
          title="Повтор фрагмента: «Відтворити фрагмент» грає його по колу, доки повтор увімкнено"
          onClick={() => {
            setLoop((on) => !on);
          }}
        >
          Повтор фрагмента
        </button>
        <button
          type="button"
          className="button"
          data-testid="player-fullscreen"
          aria-pressed={fullscreen}
          disabled={!document.fullscreenEnabled}
          onClick={toggleFullscreen}
        >
          {fullscreen ? 'Вийти з повного екрана' : 'Повний екран'}
        </button>
      </div>
    </div>
  );
}
