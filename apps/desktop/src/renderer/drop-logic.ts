/** Shown when a drop does not carry exactly one file (docs/03 section 5, ADR 024). */
export const ONE_VIDEO_MESSAGE = 'Перетягніть один відеофайл';

/** Finds a drop zone: the attribute `data-drop-zone` that `DropZone` puts on its element. */
export const DROP_ZONE_SELECTOR = '[data-drop-zone]';

/** Whether a drag carries files, not a selected text or a link dragged inside the page. */
export function carriesFiles(transfer: Pick<DataTransfer, 'types'> | null | undefined): boolean {
  return transfer?.types.includes('Files') ?? false;
}

/** The files of a drop; none when the event carries no data transfer at all. */
export function filesOf(transfer: Pick<DataTransfer, 'files'> | null | undefined): File[] {
  return transfer === null || transfer === undefined ? [] : Array.from(transfer.files);
}

/** Whether a drag event happens over a drop zone (or something inside it). */
export function isInsideDropZone(target: EventTarget | null): boolean {
  const element = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
  return (element?.closest(DROP_ZONE_SELECTOR) ?? null) !== null;
}
