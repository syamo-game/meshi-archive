import { createHash } from 'node:crypto';
import { Parser } from 'htmlparser2';
import { z } from 'zod';
import type { CandidateIdentity } from './resolution.js';
import { identitySchema, normalizeAddress, normalizeName } from './resolution.js';
import { safeHttpUrl } from './links.js';
import { externalIdentity } from './shop-creation.js';
import { casefold, escapeRegex } from './text.js';

export interface RestaurantPageEvidence { headings: readonly string[]; text_blocks: readonly string[]; visible_text: string; structured_candidates: readonly CandidateIdentity[]; page_sha256: string; }
export interface PageCandidateProof { method: 'structured_data' | 'visible_page'; page_sha256: string; candidate: CandidateIdentity; }
const postalAddress = z.object({ streetAddress: z.string().nullable().default(null), addressLocality: z.string().nullable().default(null), addressRegion: z.string().nullable().default(null), postalCode: z.string().nullable().default(null) });
const restaurant = z.object({ '@type': z.union([z.string(), z.array(z.string())]), name: z.string().min(1), address: z.union([z.string(), postalAddress]).nullable().default(null), telephone: z.string().nullable().default(null), url: z.string().nullable().default(null), servesCuisine: z.union([z.string(), z.array(z.string())]).nullable().default(null) });
type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
function* jsonNodes(value: JsonValue): Generator<{ [key: string]: JsonValue }> {
  if (Array.isArray(value)) { for (const item of value) yield* jsonNodes(item); }
  else if (typeof value === 'object' && value !== null) { yield value; if (value['@graph'] !== undefined) yield* jsonNodes(value['@graph']); }
}
export function extractJsonLdCandidates(html: string, pageUrl: string): readonly CandidateIdentity[] {
  const candidates: CandidateIdentity[] = [];
  for (const [index, block] of [...html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)].entries()) {
    let data: JsonValue;
    try { data = z.json().parse(JSON.parse(block[1]?.trim() ?? '')); }
    catch (error) { console.warn('Invalid JSON-LD block', { pageUrl, block: index + 1, error }); continue; }
    for (const node of jsonNodes(data)) {
      const parsed = restaurant.safeParse(node); if (!parsed.success) continue; const item = parsed.data;
      const types = Array.isArray(item['@type']) ? item['@type'] : [item['@type']]; if (!types.some(value => ['restaurant', 'foodestablishment', 'localbusiness'].includes(value.toLowerCase()))) continue;
      const address = typeof item.address === 'string' || item.address === null ? item.address : [item.address.postalCode, item.address.addressRegion, item.address.addressLocality, item.address.streetAddress].filter(Boolean).join(' ') || null;
      const cuisine = Array.isArray(item.servesCuisine) ? item.servesCuisine : item.servesCuisine ? [item.servesCuisine] : []; let category = cuisine.map(value => value.trim()).find(Boolean) ?? null;
      if (category && [...category].length > 255) { console.warn('JSON-LD cuisine exceeds limit', { pageUrl, length: [...category].length }); category = null; }
      let canonical = pageUrl;
      if (item.url) {
        let resolved: string | null = null;
        try { resolved = new URL(item.url, pageUrl).href; } catch (error) { console.warn('Invalid JSON-LD URL', { pageUrl, error }); }
        if (resolved && safeHttpUrl(resolved) && new URL(resolved).hostname === new URL(pageUrl).hostname) canonical = resolved;
        else console.warn('JSON-LD canonical URL is not trusted', { pageUrl });
      }
      const identity = externalIdentity(canonical);
      const candidate = identitySchema.safeParse({ name: item.name, category, address, phone: item.telephone, canonical_url: canonical, external_source: identity?.[0] ?? null, external_id: identity?.[1] ?? null, evidence_url: pageUrl });
      if (!candidate.success) { console.warn('JSON-LD restaurant does not match its contract', { pageUrl, issues: candidate.error.issues.map(issue => ({ path: issue.path, message: issue.message })) }); continue; }
      candidates.push(candidate.data);
    }
  }
  return candidates;
}
export function extractRestaurantPageEvidence(html: string, pageUrl: string): RestaurantPageEvidence {
  let skipDepth = 0; const headingTags: string[] = []; const headingParts: string[][] = []; const headings: string[] = []; const visible: string[] = [];
  const skipped = new Set(['script', 'style', 'noscript', 'template', 'svg']); const normalizeSpace = (value: string): string => value.replace(/\s+/gu, ' ').trim();
  let text = '';
  const flush = (): void => { const value = text.trim(); text = ''; if (!skipDepth && value) { visible.push(value); headingParts.at(-1)?.push(value); } };
  const parser = new Parser({
    onopentag: (tag, attributes) => {
      flush();
      if (skipped.has(tag)) { skipDepth++; return; } if (skipDepth) return;
      if (tag === 'title' || tag === 'h1') { headingTags.push(tag); headingParts.push([]); }
      if (tag === 'meta' && (attributes.property || attributes.name)?.toLowerCase() === 'og:title' && attributes.content) headings.push(attributes.content);
    },
    onclosetag: (tag, implied) => {
      if (implied) return;
      flush();
      if (skipped.has(tag)) { if (skipDepth) skipDepth--; return; }
      if (skipDepth || headingTags.at(-1) !== tag) return;
      headingTags.pop(); const heading = normalizeSpace((headingParts.pop() ?? []).join(' ')); if (heading) headings.push(heading);
    },
    ontext: value => { text += value; },
    oncomment: () => { flush(); },
    onprocessinginstruction: () => { flush(); },
  }, { decodeEntities: true, recognizeSelfClosing: true });
  parser.end(html); flush();
  const blocks = [...new Set(visible.map(normalizeSpace).filter(Boolean))];
  return { headings: [...new Set(headings)], text_blocks: blocks, visible_text: normalizeSpace(blocks.join(' ')), structured_candidates: extractJsonLdCandidates(html, pageUrl), page_sha256: createHash('sha256').update(html).digest('hex') };
}
function comparableAddress(value: string | null): string { return (normalizeAddress(value) ?? '').replace(/〒?[0-9]{3}-?[0-9]{4}/g, ''); }
export function pageAddressesMatch(left: string | null, right: string | null): boolean { const address = comparableAddress(left); return Boolean(address && address === comparableAddress(right)); }
function namePattern(value: string): RegExp {
  const name = normalizeName(value); if (!name) return /(?!)/;
  const chars = '0-9a-zぁ-んァ-ヶ一-龠ー'; return new RegExp(`(?<![${chars}])${[...name].map(escapeRegex).join(`[^${chars}]*`)}(?![${chars}])`, 'g');
}
export function pageTextMentionsRestaurantName(name: string, text: string): boolean { return namePattern(name).test(casefold(text.normalize('NFKC'))); }
export function pageTextWithoutRestaurantName(name: string, text: string): string | null { const normalized = casefold(text.normalize('NFKC')); const pattern = namePattern(name); return pattern.test(normalized) ? normalized.replace(namePattern(name), ' ') : null; }
export function pageMentionsRestaurantName(name: string, evidence: RestaurantPageEvidence): boolean {
  const normalized = normalizeName(name); return Boolean(normalized) && (evidence.structured_candidates.some(candidate => normalizeName(candidate.name) === normalized) || [...evidence.headings, ...evidence.text_blocks].some(text => pageTextMentionsRestaurantName(name, text)));
}
function addressCount(value: string): number {
  const normalized = value.normalize('NFKC'); const postal = [...normalized.matchAll(/〒?[0-9]{3}-?[0-9]{4}/g)].length; const prefectures = [...normalized.matchAll(/(?:東京都|北海道|京都府|大阪府|.{2,3}県)/g)].length;
  const address = normalizeAddress(normalized.replace(/〒?[0-9]{3}-?[0-9]{4}/g, '')) ?? ''; const street = [...address.matchAll(/(?<![0-9])[0-9]+(?:-[0-9]+){2,}(?![0-9])/g)].length;
  return Math.max(postal, prefectures, street);
}
export function provePageCandidate(candidate: CandidateIdentity, evidence: RestaurantPageEvidence): PageCandidateProof | null {
  const name = normalizeName(candidate.name); const address = comparableAddress(candidate.address); if (!name || !address) return null;
  if (evidence.structured_candidates.length) {
    const page = evidence.structured_candidates[0]; if (evidence.structured_candidates.length !== 1 || !page || normalizeName(page.name) !== name || !pageAddressesMatch(page.address, candidate.address)) return null;
    return { method: 'structured_data', page_sha256: evidence.page_sha256, candidate: page };
  }
  const bound = evidence.text_blocks.some(block => {
    if (!pageTextMentionsRestaurantName(candidate.name, block) || addressCount(block) !== 1) return false;
    const value = comparableAddress(block); if (!value || !value.endsWith(address)) return false;
    const prefix = normalizeName(value.slice(0, -address.length)); const first = prefix.indexOf(name); if (first < 0 || prefix.indexOf(name, first + name.length) >= 0) return false;
    return ['', '住所', '所在地', '店舗住所', 'address'].includes(prefix.replace(name, ''));
  });
  return bound ? { method: 'visible_page', page_sha256: evidence.page_sha256, candidate: { ...candidate, category: null, phone: null, external_source: null, external_id: null } } : null;
}
