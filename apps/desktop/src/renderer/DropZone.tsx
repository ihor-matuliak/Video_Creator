import { useEffect, useRef, useState } from 'react';
import type { DragEvent, ReactNode } from 'react';

import type { VideoChoice } from '../shared/bridge';
import { takeDroppedVideo } from './bridge-calls';
import { ONE_VIDEO_MESSAGE, carriesFiles, filesOf, isInsideDropZone } from './drop-logic';

export interface VideoDropOptions {
  /** No drop is taken while this holds (an import is under way); the zone says so. */
  readonly disabled: boolean;
  /** A drop is about to be taken: the place to forget what an earlier attempt left on screen. */
  readonly onAttempt?: (() => void) | undefined;
  /** The dropped file was accepted by main; the caller registers the video with the token. */
  readonly onVideo: (choice: VideoChoice) => void;
}

export interface VideoDrop {
  readonly disabled: boolean;
  /** A file is being dragged over the zone. */
  readonly hovering: boolean;
  /** Why the last drop was refused, in Ukrainian and without a path; null when it was not. */
  readonly error: string | null;
  readonly handlers: {
    readonly onDragEnter: (event: DragEvent<HTMLElement>) => void;
    readonly onDragOver: (event: DragEvent<HTMLElement>) => void;
    readonly onDragLeave: (event: DragEvent<HTMLElement>) => void;
    readonly onDrop: (event: DragEvent<HTMLElement>) => void;
  };
}

/**
 * Takes one video dropped on a zone (docs/03 section 5, ADR 024): exactly one file goes to
 * `takeDroppedVideo`, which gives main the path and answers with a token. Anything else, and
 * every refusal of main, becomes a short message in `error`.
 */
export function useVideoDrop({ disabled, onAttempt, onVideo }: VideoDropOptions): VideoDrop {
  const [hovering, setHovering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [wasDisabled, setWasDisabled] = useState(disabled);
  // dragenter and dragleave fire for every child the pointer crosses: only the last leave ends the hover.
  const depth = useRef(0);
  // A drop whose file is still being handed to main; a second one waits for the answer.
  const taking = useRef(false);

  // A flow that starts (the button, or a drop that was accepted) makes an old refusal stale.
  if (disabled !== wasDisabled) {
    setWasDisabled(disabled);
    if (disabled) {
      setError(null);
    }
  }

  const take = async (files: readonly File[]): Promise<void> => {
    onAttempt?.();
    const [file] = files;
    if (files.length !== 1 || file === undefined) {
      setError(ONE_VIDEO_MESSAGE);
      return;
    }
    setError(null);
    taking.current = true;
    try {
      const answer = await takeDroppedVideo(file);
      if ('canceled' in answer) {
        return;
      }
      if ('ok' in answer) {
        setError(answer.error.message);
        return;
      }
      onVideo(answer);
    } finally {
      taking.current = false;
    }
  };

  return {
    disabled,
    hovering,
    error,
    handlers: {
      onDragEnter: (event) => {
        depth.current += 1;
        if (carriesFiles(event.dataTransfer)) {
          event.preventDefault();
          setHovering(true);
        }
      },
      onDragOver: (event) => {
        if (carriesFiles(event.dataTransfer)) {
          // Without this the browser would not offer the zone as a place to drop.
          event.preventDefault();
          event.dataTransfer.dropEffect = disabled ? 'none' : 'copy';
        }
      },
      onDragLeave: () => {
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) {
          setHovering(false);
        }
      },
      onDrop: (event) => {
        event.preventDefault();
        depth.current = 0;
        setHovering(false);
        if (disabled || taking.current) {
          return;
        }
        void take(filesOf(event.dataTransfer));
      },
    },
  };
}

function zoneText(active: boolean, disabled: boolean): ReactNode {
  if (active) {
    return (
      <span data-testid="drop-zone-active">
        <span aria-hidden="true">⇩ </span>Відпустіть, щоб додати відео
      </span>
    );
  }
  return disabled
    ? 'Перетягування зараз недоступне'
    : 'Перетягніть сюди один відеофайл або скористайтеся кнопкою';
}

/** The zone: a dashed box with its hint (and the hover state in text) around the button, and the refusal under it. */
export function DropZone({ drop, children }: { readonly drop: VideoDrop; readonly children: ReactNode }) {
  const active = drop.hovering && !drop.disabled;
  return (
    <>
      <div
        className="drop-zone"
        data-testid="drop-zone"
        // Marks the zone for `isInsideDropZone` (DROP_ZONE_SELECTOR).
        data-drop-zone=""
        data-active={active}
        data-disabled={drop.disabled}
        onDragEnter={drop.handlers.onDragEnter}
        onDragOver={drop.handlers.onDragOver}
        onDragLeave={drop.handlers.onDragLeave}
        onDrop={drop.handlers.onDrop}
      >
        <p className="drop-zone-text">{zoneText(active, drop.disabled)}</p>
        {children}
      </div>
      {drop.error !== null && (
        <div role="alert" data-testid="drop-error" className="notice notice-danger">
          <p>{drop.error}</p>
        </div>
      )}
    </>
  );
}

/**
 * A file dropped anywhere but on a zone does nothing: the document cancels `dragover` and `drop`
 * so that the window never opens it (main blocks the navigation anyway), and `dragover` outside a
 * zone says «none» so that the pointer does not offer a drop there.
 */
export function useIgnoreStrayDrops(): void {
  useEffect(() => {
    const onDragOver = (event: globalThis.DragEvent): void => {
      event.preventDefault();
      if (event.dataTransfer && !isInsideDropZone(event.target)) {
        event.dataTransfer.dropEffect = 'none';
      }
    };
    const onDrop = (event: globalThis.DragEvent): void => {
      event.preventDefault();
    };
    document.addEventListener('dragover', onDragOver);
    document.addEventListener('drop', onDrop);
    return () => {
      document.removeEventListener('dragover', onDragOver);
      document.removeEventListener('drop', onDrop);
    };
  }, []);
}
