import { useId } from 'react';
import type { ReactNode, Ref } from 'react';

export type DetailsTabId = 'edit' | 'keys';

const TAB_LABELS: Readonly<Record<DetailsTabId, string>> = {
  edit: 'Межі',
  keys: 'Клавіші',
};

const TAB_ORDER: readonly DetailsTabId[] = ['edit', 'keys'];

export interface DetailsPanelProps {
  /** `id` of the panel; the splitter that resizes it points at it. */
  readonly id: string;
  readonly ref?: Ref<HTMLElement>;
  /**
   * Below 1440 px the panel sits under the player and its two parts are tabs (docs/03 section
   * 8); from 1440 px it is the right column and both parts are shown.
   */
  readonly tabbed: boolean;
  readonly active: DetailsTabId;
  readonly onChange: (tab: DetailsTabId) => void;
  /** The fields of the active clip (or the new-clip form). */
  readonly edit: ReactNode;
  /** The keyboard shortcuts. */
  readonly keys: ReactNode;
}

/**
 * The right panel of «Моменти». It is one element in both arrangements and its parts keep their
 * place in the tree, so a window that crosses 1440 px does not remount the fields: what was
 * typed stays. A part that is not shown is only hidden, never unmounted.
 */
export function DetailsPanel({ id, ref, tabbed, active, onChange, edit, keys }: DetailsPanelProps) {
  const ids = useId();
  const tabId = (tab: DetailsTabId): string => `${ids}-tab-${tab}`;
  const panelId = (tab: DetailsTabId): string => `${ids}-panel-${tab}`;
  const content: Readonly<Record<DetailsTabId, ReactNode>> = { edit, keys };

  return (
    <aside
      id={id}
      ref={ref}
      className="moments-details"
      aria-label="Панель фрагмента"
      data-testid="details-panel"
      data-tabbed={tabbed}
    >
      {tabbed && (
        <div role="tablist" aria-label="Вкладки панелі фрагмента" className="tabs">
          {TAB_ORDER.map((tab) => (
            <button
              key={tab}
              type="button"
              role="tab"
              className="tab"
              id={tabId(tab)}
              data-testid={`details-tab-${tab}`}
              aria-selected={tab === active}
              aria-controls={panelId(tab)}
              onClick={() => {
                onChange(tab);
              }}
            >
              {TAB_LABELS[tab]}
            </button>
          ))}
        </div>
      )}
      {TAB_ORDER.map((tab) => (
        <div
          key={tab}
          className="details-panel"
          id={panelId(tab)}
          role={tabbed ? 'tabpanel' : undefined}
          aria-labelledby={tabbed ? tabId(tab) : undefined}
          hidden={tabbed && tab !== active}
        >
          {content[tab]}
        </div>
      ))}
    </aside>
  );
}
