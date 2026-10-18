import { readFileSync } from 'node:fs';
import { z } from 'zod';

const overrides = z.record(z.string(), z.string()).parse(JSON.parse(readFileSync(new URL('../data/casefold.json', import.meta.url), 'utf8')));
export function casefold(value: string): string { return [...value].map(character => overrides[character] ?? character.toLowerCase()).join(''); }
export function compareText(left: string, right: string): number {
  const a = [...left]; const b = [...right];
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const diff = (a[i]?.codePointAt(0) ?? 0) - (b[i]?.codePointAt(0) ?? 0);
    if (diff) return diff;
  }
  return a.length - b.length;
}
export function quotePlus(value: string): string { return encodeURIComponent(value).replace(/[!'()*]/g, character => '%' + character.charCodeAt(0).toString(16).toUpperCase()).replace(/%20/g, '+'); }
export function escapeRegex(value: string): string { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
