import { fireEvent, render, screen } from '@testing-library/react';
import { useRef } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ActionMenu } from './ActionMenu';

function Harness({
  onSelect,
  disabled = false,
}: {
  readonly onSelect: (id: string) => void;
  readonly disabled?: boolean;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const actions = ['first', 'second', 'third'].map((id) => ({
    id,
    label: `Дія ${id}`,
    testId: `action-${id}`,
    onSelect: () => {
      onSelect(id);
    },
  }));
  return (
    <>
      <ActionMenu triggerTestId="menu" triggerRef={trigger} actions={actions} disabled={disabled} />
      <button type="button" data-testid="elsewhere">
        Деінде
      </button>
    </>
  );
}

function open(): HTMLElement {
  fireEvent.click(screen.getByTestId('menu'));
  return screen.getByRole('menu', { name: 'Ще дії' });
}

const items = (): HTMLElement[] => screen.getAllByRole('menuitem');

function item(index: number): HTMLElement {
  const found = items()[index];
  if (found === undefined) {
    throw new Error(`Немає пункту меню ${String(index)}`);
  }
  return found;
}

describe('ActionMenu', () => {
  it('is a button «Ще дії» that opens a menu of menuitems and focuses the first one', () => {
    render(<Harness onSelect={vi.fn()} />);
    const button = screen.getByRole('button', { name: 'Ще дії' });

    expect(button).toHaveAttribute('aria-haspopup', 'menu');
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    const menu = open();

    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(button).toHaveAttribute('aria-controls', menu.id);
    expect(items().map((item) => item.textContent)).toEqual(['Дія first', 'Дія second', 'Дія third']);
    expect(item(0)).toHaveFocus();
  });

  it('keeps the menu items out of the tab order, so Tab leaves the menu', () => {
    render(<Harness onSelect={vi.fn()} />);
    open();

    for (const item of items()) {
      expect(item).toHaveAttribute('tabindex', '-1');
    }
  });

  it('opens on the first item with ArrowDown and on the last one with ArrowUp', () => {
    render(<Harness onSelect={vi.fn()} />);

    fireEvent.keyDown(screen.getByTestId('menu'), { key: 'ArrowDown' });
    expect(item(0)).toHaveFocus();

    fireEvent.keyDown(item(0), { key: 'Escape' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId('menu'), { key: 'ArrowUp' });
    expect(item(2)).toHaveFocus();
  });

  it('moves between items with the arrows, Home and End, and wraps around', () => {
    render(<Harness onSelect={vi.fn()} />);
    const menu = open();

    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(item(1)).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(item(2)).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(item(0)).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowUp' });
    expect(item(2)).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'Home' });
    expect(item(0)).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'End' });
    expect(item(2)).toHaveFocus();
  });

  it('does not take other keys', () => {
    render(<Harness onSelect={vi.fn()} />);
    const menu = open();

    expect(fireEvent.keyDown(menu, { key: 'a' })).toBe(true);
    expect(item(0)).toHaveFocus();
  });

  it('closes on Escape and returns focus to the button', () => {
    render(<Harness onSelect={vi.fn()} />);
    const menu = open();

    fireEvent.keyDown(menu, { key: 'Escape' });

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByTestId('menu')).toHaveFocus();
    expect(screen.getByTestId('menu')).toHaveAttribute('aria-expanded', 'false');
  });

  it('does not let Escape reach an owner that also listens for it', () => {
    const outer = vi.fn();
    render(
      <div onKeyDown={outer}>
        <Harness onSelect={vi.fn()} />
      </div>,
    );
    const menu = open();

    fireEvent.keyDown(menu, { key: 'Escape' });

    expect(outer).not.toHaveBeenCalled();
  });

  it('closes on Tab, which then goes on to the next control', () => {
    render(<Harness onSelect={vi.fn()} />);
    const menu = open();

    expect(fireEvent.keyDown(menu, { key: 'Tab' })).toBe(true);

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when focus leaves it, but not when it moves inside', () => {
    render(<Harness onSelect={vi.fn()} />);
    open();

    fireEvent.blur(item(0), { relatedTarget: item(1) });
    expect(screen.getByRole('menu')).toBeVisible();

    fireEvent.blur(item(0), { relatedTarget: screen.getByTestId('elsewhere') });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes when focus goes nowhere, as after a click on the page background', () => {
    render(<Harness onSelect={vi.fn()} />);
    open();

    fireEvent.blur(item(0), { relatedTarget: null });

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('runs the chosen action once, closes and gives focus back to the button', () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    open();

    fireEvent.click(screen.getByTestId('action-second'));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith('second');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByTestId('menu')).toHaveFocus();
  });

  it('cannot be opened while disabled', () => {
    render(<Harness onSelect={vi.fn()} disabled />);

    fireEvent.click(screen.getByTestId('menu'));

    expect(screen.getByTestId('menu')).toBeDisabled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
