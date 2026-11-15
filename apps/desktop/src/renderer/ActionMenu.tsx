import { useEffect, useId, useRef, useState } from 'react';
import type { FocusEvent, KeyboardEvent, RefObject } from 'react';

export interface MenuAction {
  readonly id: string;
  readonly label: string;
  readonly testId: string;
  readonly onSelect: () => void;
}

/**
 * A secondary menu (docs/03 section 4): a «Ще дії» button that opens a `menu` of `menuitem`s.
 * Enter, Space and ArrowDown open it on the first item, ArrowUp on the last; the arrows, Home and
 * End move between items; Escape closes it and returns focus to the button; leaving it (Tab or a
 * click elsewhere) closes it too.
 */
export function ActionMenu({
  triggerTestId,
  triggerDescribedBy,
  triggerRef,
  actions,
  disabled = false,
}: {
  readonly triggerTestId: string;
  /** The element that says what the menu is about; the button's name stays «Ще дії». */
  readonly triggerDescribedBy?: string;
  /** The button of the menu, so the owner can return focus to it after something else closes. */
  readonly triggerRef: RefObject<HTMLButtonElement | null>;
  readonly actions: readonly MenuAction[];
  readonly disabled?: boolean;
}) {
  const menuId = useId();
  /** Where focus goes when the menu opens; null while it is closed. */
  const [openAt, setOpenAt] = useState<'first' | 'last' | null>(null);
  const menu = useRef<HTMLDivElement>(null);
  const open = openAt !== null;

  useEffect(() => {
    if (openAt === null || menu.current === null) {
      return;
    }
    const all = menu.current.querySelectorAll<HTMLElement>('[role="menuitem"]');
    (openAt === 'last' ? all[all.length - 1] : all[0])?.focus();
  }, [openAt]);

  const close = (): void => {
    setOpenAt(null);
  };

  const closeAndFocusTrigger = (): void => {
    close();
    triggerRef.current?.focus();
  };

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>): void => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setOpenAt(event.key === 'ArrowUp' ? 'last' : 'first');
    }
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeAndFocusTrigger();
      return;
    }
    if (event.key === 'Tab') {
      close();
      return;
    }
    const all = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    if (all.length === 0) {
      return;
    }
    const current = all.findIndex((item) => item === document.activeElement);
    let next: number;
    switch (event.key) {
      case 'ArrowDown':
        next = (current + 1) % all.length;
        break;
      case 'ArrowUp':
        next = (current - 1 + all.length) % all.length;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = all.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    all[next]?.focus();
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>): void => {
    const next = event.relatedTarget;
    if (!(next instanceof Node) || !event.currentTarget.contains(next)) {
      close();
    }
  };

  return (
    <div className="menu-anchor" onBlur={handleBlur}>
      <button
        ref={triggerRef}
        type="button"
        className="button"
        data-testid={triggerTestId}
        aria-describedby={triggerDescribedBy}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => {
          setOpenAt(open ? null : 'first');
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        <span aria-hidden="true">⋯</span> Ще дії
      </button>
      {open && (
        <div
          ref={menu}
          id={menuId}
          role="menu"
          aria-label="Ще дії"
          className="menu"
          onKeyDown={handleMenuKeyDown}
        >
          {actions.map((action) => (
            <button
              key={action.id}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="menu-item"
              data-testid={action.testId}
              onClick={() => {
                closeAndFocusTrigger();
                action.onSelect();
              }}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
