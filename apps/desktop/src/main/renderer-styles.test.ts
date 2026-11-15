// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

// Lives next to renderer-html.test.ts: the stylesheet is read from disk, which the renderer's
// own TypeScript project (browser types, no Node) cannot do.
const CSS = readFileSync(resolve(import.meta.dirname, '../renderer/styles.css'), 'utf8');

// docs/03 section 2: "check the actual contrasts; the palette is a starting design, not proof of
// WCAG". This reads styles.css itself, so a new colour pair that is not readable fails here
// without anyone adding it to a list.

const WCAG_AA_TEXT = 4.5;

interface Rule {
  readonly selector: string;
  readonly declarations: ReadonlyMap<string, string>;
}

function parseRules(css: string): Rule[] {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules: Rule[] = [];
  for (const [, selectors = '', body = ''] of withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const declarations = new Map<string, string>();
    for (const declaration of body.split(';')) {
      const colon = declaration.indexOf(':');
      if (colon > 0) {
        declarations.set(declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim());
      }
    }
    for (const selector of selectors.split(',')) {
      rules.push({ selector: selector.trim().replace(/\s+/g, ' '), declarations });
    }
  }
  return rules;
}

const RULES = parseRules(CSS);

const TOKENS: ReadonlyMap<string, string> = new Map(
  [...(RULES.find((rule) => rule.selector === ':root')?.declarations ?? [])].filter(([name]) =>
    name.startsWith('--'),
  ),
);

/** A colour value of a declaration as #rrggbb; null for transparent, inherit and the like. */
function resolveColor(value: string | undefined): string | null {
  if (value === undefined) {
    return null;
  }
  const token = /^var\((--[\w-]+)\)$/.exec(value);
  const literal = token === null ? value : (TOKENS.get(token[1] ?? '') ?? '');
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(literal);
  if (hex === null) {
    return null;
  }
  const digits = hex[1] ?? '';
  const full = digits.length === 3 ? digits.replace(/./g, '$&$&') : digits;
  return `#${full.toLowerCase()}`;
}

function channel(hex: string, offset: number): number {
  const value = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
  return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  return 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5);
}

/** WCAG 2.x contrast ratio of two #rrggbb colours. */
function contrast(first: string, second: string): number {
  const [light = 0, dark = 0] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

const IS_STATE = /:(?:hover|focus-visible|focus|active|disabled|not\([^)]*\))/;
const STATE = /:(?:hover|focus-visible|focus|active|disabled|not\([^)]*\))/g;

interface Pair {
  readonly text: string;
  readonly background: string;
  readonly where: string;
}

/**
 * Every text colour on every background it can have. A rule that sets both colours is a pair.
 * A rule that sets only a text colour sits on any of the plain surfaces (a rule that sets only
 * a background, plus the page's). A hover or disabled rule that sets only a background keeps the
 * text colour of the rule it modifies.
 */
function collectPairs(): Pair[] {
  const rules = RULES.filter((rule) => rule.selector !== ':root' && !rule.selector.includes('::'));
  const pairs: Pair[] = [];
  const texts = new Map<string, string>();
  const surfaces = new Map<string, string>();

  for (const rule of rules) {
    const text = resolveColor(rule.declarations.get('color'));
    const background = resolveColor(
      rule.declarations.get('background') ?? rule.declarations.get('background-color'),
    );
    const modifies = IS_STATE.test(rule.selector);
    if (text !== null && background !== null) {
      pairs.push({ text, background, where: rule.selector });
      if (rule.selector === 'body') {
        surfaces.set(background, rule.selector);
      }
    } else if (text !== null) {
      texts.set(text, rule.selector);
    } else if (background !== null) {
      if (modifies) {
        const base = rule.selector.replace(STATE, '');
        const inherited = rules
          .filter((other) => other.selector === base)
          .map((other) => resolveColor(other.declarations.get('color')))
          .find((color) => color !== null);
        pairs.push({ text: inherited ?? resolveColor('var(--text)') ?? '#ffffff', background, where: rule.selector });
      } else {
        surfaces.set(background, rule.selector);
      }
    }
  }
  for (const [text, textWhere] of texts) {
    for (const [background, surfaceWhere] of surfaces) {
      pairs.push({ text, background, where: `${textWhere} on ${surfaceWhere}` });
    }
  }
  return pairs;
}

const PAIRS = collectPairs();

/** The colour of a token; a token that does not resolve is a failure of the test, not a null. */
function token(name: string): string {
  const color = resolveColor(`var(${name})`);
  if (color === null) {
    throw new Error(`${name} is not a colour in styles.css`);
  }
  return color;
}

describe('styles.css contrast (WCAG AA, 4.5:1)', () => {
  it('defines the palette of docs/03 section 2 as tokens', () => {
    for (const name of [
      '--bg',
      '--surface',
      '--raised',
      '--text',
      '--text-secondary',
      '--accent',
      '--success',
      '--warning',
      '--danger',
    ]) {
      expect(resolveColor(`var(${name})`), name).not.toBeNull();
    }
  });

  it('finds the pairs it is meant to check (a parser that finds nothing would pass anything)', () => {
    const labels = PAIRS.map((pair) => `${pair.text}/${pair.background}`);

    // The primary button: dark text on the accent; the page: light text on the dark background.
    expect(labels).toContain(`${token('--bg')}/${token('--accent')}`);
    expect(labels).toContain(`${token('--text')}/${token('--bg')}`);
    // Statuses on each plain surface.
    for (const status of ['--success', '--warning', '--danger', '--text-secondary']) {
      for (const surface of ['--bg', '--surface', '--raised']) {
        expect(labels, `${status} on ${surface}`).toContain(
          `${token(status)}/${token(surface)}`,
        );
      }
    }
    expect(PAIRS.length).toBeGreaterThan(30);
  });

  it('computes the ratio the way WCAG does', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrast('#777777', '#ffffff')).toBeCloseTo(4.48, 2);
    expect(contrast('#ffffff', '#ffffff')).toBe(1);
  });

  it('gives every text and background pair of the stylesheet at least 4.5:1', () => {
    const failing = PAIRS.map((pair) => ({ ...pair, ratio: contrast(pair.text, pair.background) }))
      .filter((pair) => pair.ratio < WCAG_AA_TEXT)
      .map((pair) => `${pair.where}: ${pair.text} on ${pair.background} = ${pair.ratio.toFixed(2)}`);

    expect(failing).toEqual([]);
  });
});
