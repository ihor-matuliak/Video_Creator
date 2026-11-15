import type { ReactNode } from 'react';

export function TestBadge() {
  return (
    <span className="badge" data-testid="test-badge">
      Тестові дані
    </span>
  );
}

export type Tone = 'success' | 'warning' | 'danger' | 'neutral';

const TONE_ICONS: Readonly<Record<Tone, string>> = {
  success: '✓',
  warning: '⚠',
  danger: '✕',
  neutral: '◌',
};

/** A status is always an icon plus text, never colour alone (docs/03 section 2). */
export function StatusText({
  tone,
  testId,
  children,
}: {
  readonly tone: Tone;
  readonly testId?: string;
  readonly children: ReactNode;
}) {
  return (
    <span className={`status status-${tone}`} data-testid={testId}>
      <span aria-hidden="true">{TONE_ICONS[tone]}</span> {children}
    </span>
  );
}
