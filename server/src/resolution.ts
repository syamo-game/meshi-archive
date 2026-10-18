import { z } from 'zod';
import { canonicalArea } from './geography.js';
import { safeHttpUrl } from './links.js';
import { casefold } from './text.js';

const nonWord = /[^0-9a-zぁ-んァ-ヶ一-龠ー]+/gi;
const externalAliases: Readonly<Record<string, string>> = { 食べログ: 'tabelog', tabelogcom: 'tabelog', wwwtabelogcom: 'tabelog', ホットペッパー: 'hotpepper', hotpepperjp: 'hotpepper', wwwhotpepperjp: 'hotpepper', ぐるなび: 'gnavi', gnavicojp: 'gnavi', wwwgnavicojp: 'gnavi', gurunavi: 'gnavi', gurunavicom: 'gnavi', wwwgurunavicom: 'gnavi', 一休: 'ikyu', ikyucom: 'ikyu', wwwikyucom: 'ikyu', retty: 'retty', rettyme: 'retty', wwwrettyme: 'retty' };
const optional = (maximum: number): z.ZodNullable<z.ZodDefault<z.ZodString>> => z.string().trim().max(maximum).default('').nullable();
const candidateUrl = z.string().trim().max(2048).refine(value => safeHttpUrl(value) !== null, 'URL must use HTTP or HTTPS without credentials').nullable().default(null);
export const identitySchema = z.strictObject({ name: z.string().trim().min(1).max(500), area: optional(255).default(null), category: optional(255).default(null), address: optional(2000).default(null), phone: optional(64).default(null), canonical_url: candidateUrl, external_source: optional(64).default(null), external_id: optional(255).default(null), evidence_url: candidateUrl });
export type CandidateIdentity = z.infer<typeof identitySchema>;
export interface ResolutionOutcome { is_strong_match: boolean; name_similarity: number; matched_fields: string[]; conflicting_fields: string[]; reason: string; }
export function normalizeName(value: string): string { return value.normalize('NFKC').toLowerCase().trim().replace(/^(?:株式会社|有限会社|合同会社)/, '').replace(nonWord, ''); }
export function normalizeArea(value: string | null): string { return (canonicalArea(value) ?? value ?? '').normalize('NFKC').toLowerCase().trim().replace(nonWord, ''); }
export function normalizeExternalSource(value: string | null): string | null { const normalized = value ? normalizeName(value) : ''; return normalized ? externalAliases[normalized] ?? normalized : null; }
export function normalizeExternalIdentity(source: string | null, id: string | null): readonly [string, string] | null { const normalizedSource = normalizeExternalSource(source); const normalizedId = id ? casefold(id.normalize('NFKC').trim()) : ''; return normalizedSource && normalizedId ? [normalizedSource, normalizedId] : null; }
export function normalizeUrlIdentity(value: string | null): string | null {
  if (!value) return null;
  const cleaned = value.trim();
  if (!safeHttpUrl(cleaned)) throw new Error('Canonical URL must use HTTP or HTTPS without credentials');
  const parsed = new URL(cleaned); const raw = /^https?:\/\/[^/?#]+([^?#]*)(\?[^#]*)?/i.exec(cleaned);
  const originalPath = raw?.[1] ?? ''; const parameterIndex = originalPath.lastIndexOf(';');
  const hasParameters = parameterIndex > originalPath.lastIndexOf('/');
  const path = (hasParameters ? originalPath.slice(0, parameterIndex) : originalPath).replace(/\/+$/, '') || '/';
  return `${parsed.protocol}//${parsed.host}${path}${hasParameters ? originalPath.slice(parameterIndex) : ''}${raw?.[2] ?? ''}`;
}
export function extractBranchToken(value: string): string | null {
  const normalized = value.normalize('NFKC').trim().replace(/[)\]}」』】〉》〕〗〙〛]+$/, '');
  const token = /([^\s　]+(?:本店|支店|店))$/.exec(normalized)?.[1];
  if (token === undefined) return null;
  let opening = -1;
  for (const character of '([{「『【〈《〔〖〘〚') opening = Math.max(opening, token.lastIndexOf(character));
  return normalizeName(token.slice(opening + 1));
}
export function branchesConflict(left: string, right: string): boolean { const a = extractBranchToken(left); const b = extractBranchToken(right); return Boolean(a && b && a !== b); }
export function normalizePhone(value: string | null): string | null { let digits = (value ?? '').normalize('NFKC').replace(/[^\p{Decimal_Number}]/gu, ''); if (digits.startsWith('81') && digits.length >= 11) digits = '0' + digits.slice(2); return digits || null; }
export function normalizeAddress(value: string | null): string | null { return (value ?? '').normalize('NFKC').toLowerCase().replace(/丁目|番地|番/g, '-').replace(/号/g, '').replace(/[\s　,，.。・]/gu, '').replace(/-+/g, '-').replace(/^-|-$/g, '') || null; }

export function nameSimilarity(left: string, right: string): number {
  const a = [...normalizeName(left)]; const b = [...normalizeName(right)];
  if (!a.length && !b.length) return 1;
  const positions = new Map<string, number[]>();
  b.forEach((character, index) => { const found = positions.get(character) ?? []; found.push(index); positions.set(character, found); });
  if (b.length >= 200) for (const [character, indexes] of positions) if (indexes.length > Math.floor(b.length / 100) + 1) positions.delete(character);
  const pending: [number, number, number, number][] = [[0, a.length, 0, b.length]];
  let matched = 0;
  while (pending.length) {
    const bounds = pending.pop(); if (!bounds) throw new Error('Sequence matcher stack was unexpectedly empty');
    const [alo, ahi, blo, bhi] = bounds;
    let bestA = alo; let bestB = blo; let best = 0; let previous = new Map<number, number>();
    for (let i = alo; i < ahi; i++) {
      const current = new Map<number, number>();
      for (const j of positions.get(a[i] ?? '') ?? []) {
        if (j < blo) continue; if (j >= bhi) break;
        const size = (previous.get(j - 1) ?? 0) + 1; current.set(j, size);
        if (size > best) { bestA = i - size + 1; bestB = j - size + 1; best = size; }
      }
      previous = current;
    }
    while (bestA > alo && bestB > blo && a[bestA - 1] === b[bestB - 1]) { bestA--; bestB--; best++; }
    while (bestA + best < ahi && bestB + best < bhi && a[bestA + best] === b[bestB + best]) best++;
    if (!best) continue;
    matched += best;
    if (alo < bestA && blo < bestB) pending.push([alo, bestA, blo, bestB]);
    if (bestA + best < ahi && bestB + best < bhi) pending.push([bestA + best, ahi, bestB + best, bhi]);
  }
  return 2 * matched / (a.length + b.length);
}
export function evaluateIdentity(existing: CandidateIdentity, candidate: CandidateIdentity): ResolutionOutcome {
  const similarity = nameSimilarity(existing.name, candidate.name); const matched: string[] = []; const conflicts: string[] = [];
  const branchConflict = branchesConflict(existing.name, candidate.name); if (branchConflict) conflicts.push('branch');
  const a = normalizeExternalIdentity(existing.external_source, existing.external_id); const b = normalizeExternalIdentity(candidate.external_source, candidate.external_id);
  const sameExternal = Boolean(a && b && a[0] === b[0] && a[1] === b[1]); const externalConflict = Boolean(a && b && a[0] === b[0] && a[1] !== b[1]);
  if (sameExternal) matched.push('external_id'); else if (externalConflict) conflicts.push('external_id');
  const phoneA = normalizePhone(existing.phone); const phoneB = normalizePhone(candidate.phone); const samePhone = Boolean(phoneA && phoneB && phoneA === phoneB);
  if (samePhone) matched.push('phone'); else if (phoneA && phoneB) conflicts.push('phone');
  const addressA = normalizeAddress(existing.address); const addressB = normalizeAddress(candidate.address); const sameAddress = Boolean(addressA && addressB && addressA === addressB);
  if (sameAddress) matched.push('address'); else if (addressA && addressB) conflicts.push('address');
  if (similarity >= 0.8) matched.push('name'); else conflicts.push('name');
  const strong = !branchConflict && !externalConflict && (sameExternal || (samePhone && similarity >= 0.8) || (sameAddress && similarity >= 0.9));
  const reason = branchConflict ? '支店名が矛盾' : externalConflict ? '同一サービスの外部IDが矛盾' : sameExternal ? '同一サービスの外部IDが一致' : samePhone && similarity >= 0.8 ? '電話番号が一致し、店名の類似度が0.80以上' : sameAddress && similarity >= 0.9 ? '住所が一致し、店名の類似度が0.90以上' : '自動確定に必要な強い一致根拠が不足';
  return { is_strong_match: strong, name_similarity: similarity, matched_fields: matched, conflicting_fields: conflicts, reason };
}
