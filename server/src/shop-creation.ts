import { createHash } from 'node:crypto';
import type { Executor } from './db/database.js';
import { canonicalArea } from './geography.js';
import { shopRecord } from './records.js';
import { evaluateIdentity, normalizeExternalIdentity, normalizeName, normalizeUrlIdentity } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { casefold } from './text.js';

export function externalIdentity(url: string | null): readonly [string, string] | null {
  if (!url) return null;
  const parsed = new URL(url); const host = parsed.hostname.toLowerCase(); const path = parsed.pathname.replace(/\/+$/, '');
  const matches = (domain: string): boolean => host === domain || host.endsWith('.' + domain);
  if (matches('tabelog.com')) { const id = /\/(\d{6,})(?:\/|$)/.exec(path + '/')?.[1]; return id ? ['tabelog', id] : null; }
  if (matches('hotpepper.jp')) { const id = /\/(str[^/]+)(?:\/|$)/i.exec(path + '/')?.[1]; return id ? ['hotpepper', id.toLowerCase()] : null; }
  if (matches('gnavi.co.jp')) { const id = path.replace(/^\/+/, '').split('/')[0]; return id && /^(?=.*[0-9])[a-z0-9_-]{5,}$/i.test(id) ? ['gnavi', casefold(id)] : null; }
  if (matches('ikyu.com')) { const id = /\/restaurant\/([^/]+)/.exec(path)?.[1]; return id ? ['ikyu', id] : null; }
  if (matches('retty.me')) { const id = /\/stores\/([^/]+)/i.exec(path)?.[1]; return id ? ['retty', casefold(id)] : null; }
  return null;
}
export function collectExternalIdentities(source: string | null, id: string | null, urls: readonly (string | null)[] = []): ReadonlyMap<string, ReadonlySet<string>> {
  const result = new Map<string, Set<string>>();
  const add = (identity: readonly [string, string] | null): void => { if (!identity) return; const ids = result.get(identity[0]) ?? new Set<string>(); ids.add(identity[1]); result.set(identity[0], ids); };
  add(normalizeExternalIdentity(source, id)); for (const url of urls) { const identity = externalIdentity(url); if (identity) add(normalizeExternalIdentity(...identity)); }
  return result;
}
export function identitiesConflict(identities: ReadonlyMap<string, ReadonlySet<string>>): boolean { return [...identities.values()].some(ids => ids.size > 1); }
export async function lockShopCreation(connection: Executor): Promise<void> {
  if (connection.dialect === 'postgres') await connection.execute('SELECT pg_advisory_xact_lock($1::bigint)', [createHash('sha256').update('meshi-archive:new-shop-creation').digest().readBigInt64BE().toString()]);
}
export interface ShopCollision { kind: 'external_id' | 'canonical_url' | 'name_area' | 'strong_identity'; shop_id: number; matched_by: 'external_id' | 'canonical_url' | 'evidence_url' | 'name_area' | 'strong_identity'; }
export interface NewShopIdentity { shop_name: string; branch_name: string | null; area: string | null; address: string | null; phone: string | null; canonical_url: string | null; evidence_url: string | null; external_source: string | null; external_id: string | null; }
export async function findShopCollisions(connection: Executor, input: NewShopIdentity, exclude: number | null = null): Promise<readonly ShopCollision[]> {
  const shops = (await connection.query('SELECT * FROM shops ORDER BY id', shopRecord)).filter(shop => shop.id !== exclude);
  const collisions: ShopCollision[] = []; const seen = new Set<string>();
  const append = (kind: ShopCollision['kind'], id: number, by: ShopCollision['matched_by']): void => { const key = `${kind}:${id}:${by}`; if (!seen.has(key)) { seen.add(key); collisions.push({ kind, shop_id: id, matched_by: by }); } };
  const identities = collectExternalIdentities(input.external_source, input.external_id, [input.canonical_url, input.evidence_url]);
  for (const shop of shops) {
    const existing = collectExternalIdentities(shop.external_source, shop.external_id, [shop.canonical_url]);
    if ([...identities].some(([source, ids]) => [...ids].some(id => existing.get(source)?.has(id)))) append('external_id', shop.id, 'external_id');
  }
  const canonical = normalizeUrlIdentity(input.canonical_url); const evidence = normalizeUrlIdentity(input.evidence_url);
  for (const shop of shops) {
    const url = normalizeUrlIdentity(shop.canonical_url); if (!url) continue;
    if (canonical === url) append('canonical_url', shop.id, 'canonical_url');
    if (evidence === url) append('canonical_url', shop.id, 'evidence_url');
  }
  const name = (value: string, branch: string | null): string => { const normalized = normalizeName(value); const suffix = normalizeName(branch ?? ''); return suffix && !normalized.endsWith(suffix) ? normalized + suffix : normalized; };
  for (const shop of shops) if (name(shop.shop_name, shop.branch_name) === name(input.shop_name, input.branch_name) && canonicalArea(shop.area) === canonicalArea(input.area)) append('name_area', shop.id, 'name_area');
  const identity: CandidateIdentity = { name: [input.shop_name, input.branch_name].filter(Boolean).join(' ').trim(), area: canonicalArea(input.area), category: null, address: input.address, phone: input.phone, external_source: input.external_source, external_id: input.external_id, canonical_url: null, evidence_url: null };
  for (const shop of shops) {
    const existing: CandidateIdentity = { name: [shop.shop_name, shop.branch_name].filter(Boolean).join(' ').trim(), area: shop.area, category: null, address: shop.address, phone: shop.phone, external_source: shop.external_source, external_id: shop.external_id, canonical_url: null, evidence_url: null };
    if (evaluateIdentity(existing, identity).is_strong_match) append('strong_identity', shop.id, 'strong_identity');
  }
  return collisions;
}
