import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export interface IpcSender {
  /** webContents id of the sender. */
  readonly webContentsId: number;
  /** URL of the frame that sent the message; null when the frame is gone. */
  readonly frameUrl: string | null;
  /** True only for the top-level frame of the sender. */
  readonly isMainFrame: boolean;
}

export interface TrustedPage {
  readonly webContentsId: number;
  /** URL the app itself loaded into that window. */
  readonly pageUrl: string;
}

function samePath(a: string, b: string): boolean {
  const left = resolve(a);
  const right = resolve(b);
  return process.platform === 'win32' ? left.toLowerCase() === right.toLowerCase() : left === right;
}

/** Compares URLs ignoring query and hash; file URLs by their filesystem path. */
export function isOwnPageUrl(candidate: string, ownPage: string): boolean {
  try {
    const left = new URL(candidate);
    const right = new URL(ownPage);
    if (left.protocol !== right.protocol) {
      return false;
    }
    if (left.protocol === 'file:') {
      return samePath(fileURLToPath(left), fileURLToPath(right));
    }
    return left.origin === right.origin && left.pathname === right.pathname;
  } catch {
    return false;
  }
}

/** True only for the top frame of the app's own window showing the app's own page. */
export function isTrustedSender(sender: IpcSender, trusted: TrustedPage | null): boolean {
  return (
    trusted !== null &&
    sender.webContentsId === trusted.webContentsId &&
    sender.isMainFrame &&
    sender.frameUrl !== null &&
    isOwnPageUrl(sender.frameUrl, trusted.pageUrl)
  );
}
