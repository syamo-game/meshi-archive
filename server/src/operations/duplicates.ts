import { z } from 'zod';
import { nameWithBranch } from '../candidate-geography.js';
import type { Database, Executor } from '../db/database.js';
import { shopIdentities } from '../pipeline-shops.js';
import { mentionRecord, shopRecord } from '../records.js';
import type { ShopRecord } from '../records.js';
import { mergeSnapshot } from '../review-decisions.js';
import { branchesConflict, extractBranchToken, normalizeAddress, normalizeArea, normalizeName, normalizePhone, normalizeUrlIdentity } from '../resolution.js';
import { collectExternalIdentities, identitiesConflict, lockShopCreation } from '../shop-creation.js';
import { compareText } from '../text.js';

interface ShadowGroup { normalized_name: string; normalized_branch: string; normalized_area: string; shop_ids: number[]; evidence: { phone_match: boolean; address_match: boolean }; identity_conflicts: string[]; user_data_conflicts: string[]; would_auto_merge: boolean; }
export interface MergeGroup { shop_ids: number[]; keep_shop_id: number; evidence: string[]; is_safe: boolean; reason: string; identity_conflicts: string[]; user_data_conflicts: string[]; }
interface MergeResult { shop_ids: number[]; keep_shop_id: number; evidence: string[]; action: 'merged' | 'skipped'; reason: string; merged_shop_count: number; moved_mention_count: number; complemented_fields: string[]; }
export interface DuplicateReport { groups: ShadowGroup[]; candidate_group_count: number; candidate_shop_count: number; auto_merge_group_count: number; safe_merge_plan: { groups: MergeGroup[] }; safe_merge_group_count: number; safe_merge_shop_count: number; safe_merge_result: { groups: MergeResult[] } | null; merged_group_count: number; merged_shop_count: number; moved_mention_count: number; }
const redirectRow = z.object({ source_shop_id: z.number(), target_shop_id: z.number() });
const branch = (shop: ShopRecord): string => normalizeName(shop.branch_name || extractBranchToken(shop.shop_name) || '');
function distinct(values: readonly (string | number | boolean | null)[]): number { return new Set(values.filter(value => value !== null)).size; }
function pairs<T>(values: readonly T[]): [T, T][] { return values.flatMap((left, index) => values.slice(index + 1).map(right => [left, right] as [T, T])); }
function externalKeys(shop: ShopRecord): string[] { return [...shopIdentities(shop)].flatMap(([source, ids]) => [...ids].map(id => source + ':' + id)); }
function userConflicts(shops: readonly ShopRecord[], safe = false): string[] {
  const result = (['is_visited', 'visited_at', 'rating', 'memo', 'image_key'] as const).filter(field => distinct(shops.map(shop => shop[field])) > 1) as string[];
  if (safe && shops.some(shop => !shop.is_visited && shop.visited_at !== null)) result.push('visit_state'); return result;
}
function identityConflicts(shops: readonly ShopRecord[]): string[] {
  const result = new Set<string>(); const combined = new Map<string, Set<string>>(); const explicit = new Set<string>();
  for (const shop of shops) {
    for (const [source, ids] of shopIdentities(shop)) { const group = combined.get(source) ?? new Set<string>(); for (const id of ids) group.add(id); combined.set(source, group); }
    for (const [source, ids] of collectExternalIdentities(shop.external_source, shop.external_id)) for (const id of ids) explicit.add(source + ':' + id);
  }
  if (identitiesConflict(combined) || explicit.size > 1) result.add('external_identity');
  for (const [left, right] of pairs(shops)) {
    const a = branch(left); const b = branch(right); const leftName = nameWithBranch(left.shop_name, left.branch_name); const rightName = nameWithBranch(right.shop_name, right.branch_name);
    if ((a && b && a !== b) || branchesConflict(leftName, rightName)) result.add('branch_name');
    const first = normalizeName(leftName); const second = normalizeName(rightName);
    if (!first || !second || !(first === second || (Math.min([...first].length, [...second].length) >= 4 && (first.includes(second) || second.includes(first))))) result.add('shop_name');
    if (normalizePhone(left.phone) && normalizePhone(left.phone) === normalizePhone(right.phone) && normalizeAddress(left.address) && normalizeAddress(left.address) === normalizeAddress(right.address) && Boolean(a) !== Boolean(b) && !externalKeys(left).some(key => externalKeys(right).includes(key))) result.add('branch_name');
  }
  const fields: readonly [string, (shop: ShopRecord) => string | null][] = [['area', shop => normalizeArea(shop.area)], ['category', shop => normalizeName(shop.category ?? '')], ['address', shop => normalizeAddress(shop.address)], ['phone', shop => normalizePhone(shop.phone)], ['canonical_url', shop => normalizeUrlIdentity(shop.canonical_url)]];
  for (const [field, normalize] of fields) if (new Set(shops.map(normalize).filter(Boolean)).size > 1) result.add(field);
  return [...result].sort(compareText);
}
function components(shops: readonly ShopRecord[]): { shops: ShopRecord[]; evidence: string[] }[] {
  const parents = new Map(shops.map(shop => [shop.id, shop.id])); const edges: { left: number; right: number; evidence: string }[] = []; const byEvidence = new Map<string, ShopRecord[]>();
  const root = (id: number): number => { const parent = parents.get(id); if (parent === undefined) throw new Error(`Duplicate component has no parent: shop_id=${id}`); return parent === id ? id : root(parent); };
  for (const shop of shops) {
    const phone = normalizePhone(shop.phone); const address = normalizeAddress(shop.address);
    const keys = [...externalKeys(shop).map(key => 'external_id:' + key), ...(phone && address ? ['phone_address:' + phone + ':' + address] : [])];
    for (const key of keys) { const group = byEvidence.get(key) ?? []; group.push(shop); byEvidence.set(key, group); }
  }
  for (const [key, members] of byEvidence) for (const [left, right] of pairs(members)) { const a = root(left.id); const b = root(right.id); if (a !== b) parents.set(Math.max(a, b), Math.min(a, b)); edges.push({ left: left.id, right: right.id, evidence: key.startsWith('external_id:') ? 'external_id' : 'phone_address' }); }
  const grouped = new Map<number, ShopRecord[]>(); for (const shop of shops) { const key = root(shop.id); const group = grouped.get(key) ?? []; group.push(shop); grouped.set(key, group); }
  return [...grouped.values()].filter(group => group.length > 1).map(group => ({ shops: group.sort((a, b) => a.id - b.id), evidence: [...new Set(edges.filter(edge => group.some(shop => shop.id === edge.left) && group.some(shop => shop.id === edge.right)).map(edge => edge.evidence))].sort(compareText) })).sort((a, b) => (a.shops[0]?.id ?? 0) - (b.shops[0]?.id ?? 0));
}
async function plan(connection: Executor, shops: readonly ShopRecord[]): Promise<MergeGroup[]> {
  const redirects = await connection.query('SELECT source_shop_id,target_shop_id FROM shop_redirects', redirectRow); const live = new Set((await connection.query('SELECT id FROM shops', z.object({ id: z.number() }))).map(shop => shop.id));
  return components(shops).map(component => {
    const keeper = component.shops[0]; if (!keeper) throw new Error('Duplicate component is empty'); const ids = component.shops.map(shop => shop.id); const losing = new Set(ids.slice(1));
    const identity = identityConflicts(component.shops); const user = userConflicts(component.shops, true);
    const redirect = redirects.some(row => row.source_shop_id === keeper.id && losing.has(row.target_shop_id)) ? 'redirect_conflict:keeper_would_redirect_to_itself' : redirects.some(row => live.has(row.source_shop_id) && !losing.has(row.source_shop_id) && losing.has(row.target_shop_id)) ? 'redirect_conflict:live_shop_points_to_losing_shop' : null;
    return { shop_ids: ids, keep_shop_id: keeper.id, evidence: component.evidence, is_safe: !identity.length && !user.length && !redirect, reason: identity.length ? 'identity_conflict:' + identity.join(',') : user.length ? 'user_data_conflict:' + user.join(',') : redirect || 'safe_evidence:' + component.evidence.join(','), identity_conflicts: identity, user_data_conflicts: user };
  });
}
function shadow(shops: readonly ShopRecord[]): ShadowGroup[] {
  const grouped = new Map<string, { key: readonly [string, string, string]; shops: ShopRecord[] }>();
  for (const shop of shops) { const suffix = branch(shop); let name = normalizeName(shop.shop_name); if (suffix && !name.endsWith(suffix)) name += suffix; const area = normalizeArea(shop.area); if (!name || !area) continue; const key = [name, suffix, area] as const; const encoded = JSON.stringify(key); const group = grouped.get(encoded) ?? { key, shops: [] }; group.shops.push(shop); grouped.set(encoded, group); }
  return [...grouped.values()].filter(group => group.shops.length > 1).sort((a, b) => compareText(a.key.join('\0'), b.key.join('\0'))).map(({ key, shops: members }) => {
    const allEqual = (values: readonly (string | null)[]): boolean => values.every(Boolean) && new Set(values).size === 1;
    const evidence = { phone_match: allEqual(members.map(shop => normalizePhone(shop.phone))), address_match: allEqual(members.map(shop => normalizeAddress(shop.address))) };
    const identity: string[] = []; if (distinct(members.map(shop => shop.external_source && shop.external_id ? JSON.stringify([shop.external_source, shop.external_id]) : null)) > 1) identity.push('external_identity'); if (distinct(members.map(shop => normalizePhone(shop.phone))) > 1) identity.push('phone'); if (distinct(members.map(shop => normalizeAddress(shop.address))) > 1) identity.push('address'); const user = userConflicts(members);
    return { normalized_name: key[0], normalized_branch: key[1], normalized_area: key[2], shop_ids: members.map(shop => shop.id), evidence, identity_conflicts: identity, user_data_conflicts: user, would_auto_merge: (evidence.phone_match || evidence.address_match) && !identity.length && !user.length };
  });
}
async function merge(connection: Executor, group: MergeGroup, allShops: readonly ShopRecord[]): Promise<MergeResult> {
  const result: MergeResult = { shop_ids: group.shop_ids, keep_shop_id: group.keep_shop_id, evidence: group.evidence, action: 'skipped', reason: group.reason, merged_shop_count: 0, moved_mention_count: 0, complemented_fields: [] };
  if (!group.is_safe) return result;
  const members = group.shop_ids.map(id => { const shop = allShops.find(item => item.id === id); if (!shop) throw new Error(`Duplicate shop disappeared: shop_id=${id}`); return shop; }); const keeper = members[0]; if (!keeper) throw new Error('Duplicate keeper is missing'); const losing = members.slice(1);
  const markers = group.shop_ids.map((_, index) => '$' + (index + 1)).join(','); const mentions = await connection.query(`SELECT * FROM shop_mentions WHERE shop_id IN (${markers}) ORDER BY id${connection.dialect === 'postgres' ? ' FOR UPDATE' : ''}`, mentionRecord, group.shop_ids);
  const approved = new Set(mentions.filter(row => row.review_status === 'approved').map(row => row.shop_id)); if (group.shop_ids.some(id => !approved.has(id))) return { ...result, reason: 'approved_mentions_changed' };
  const moved = mentions.filter(row => losing.some(shop => shop.id === row.shop_id)); const note = mergeSnapshot(members, moved, 'safe duplicate merge; evidence=' + group.evidence.join(',')); const updated = { ...keeper }; const fields: string[] = [];
  for (const field of ['branch_name', 'area', 'category', 'address', 'phone', 'canonical_url'] as const) if (!updated[field]) { const value = members.find(shop => shop[field])?.[field]; if (value) { updated[field] = value; fields.push(field); } }
  if (updated.image_key === null) { const image = members.find(shop => shop.image_key)?.image_key; if (image) { updated.image_key = image; fields.push('image_key'); } }
  if (!updated.external_source && !updated.external_id) { const external = members.find(shop => shop.external_source && shop.external_id); if (external) { for (const shop of losing) await connection.execute('UPDATE shops SET external_source=NULL,external_id=NULL WHERE id=$1', [shop.id]); updated.external_source = external.external_source; updated.external_id = external.external_id; fields.push('external_identity'); } }
  if (updated.is_visited && updated.visited_at === null) { const date = members.find(shop => shop.visited_at)?.visited_at; if (date) { updated.visited_at = date; fields.push('visited_at'); } }
  if (updated.rating === null) { const rating = members.find(shop => shop.rating !== null)?.rating; if (rating !== undefined && rating !== null) { updated.rating = rating; fields.push('rating'); } }
  if (updated.memo === null) { const memo = members.find(shop => shop.memo !== null)?.memo; if (memo !== undefined && memo !== null) { updated.memo = memo; fields.push('memo'); } }
  const now = new Date().toISOString();
  await connection.execute('UPDATE shops SET branch_name=$1,area=$2,category=$3,address=$4,phone=$5,canonical_url=$6,image_key=$7,external_source=$8,external_id=$9,visited_at=$10,rating=$11,memo=$12,is_visited=$13,version=version+1,updated_at=$14 WHERE id=$15', [updated.branch_name, updated.area, updated.category, updated.address, updated.phone, updated.canonical_url, updated.image_key, updated.external_source, updated.external_id, updated.visited_at, updated.rating, updated.memo, updated.rating !== null, now, keeper.id]);
  for (const mention of moved) { await connection.execute('UPDATE shop_mentions SET shop_id=$1,version=version+1 WHERE id=$2', [keeper.id, mention.id]); await connection.execute("INSERT INTO review_events(mention_id,scope,action,previous_shop_id,selected_shop_id,note,created_at) VALUES($1,'identity','automatic_merge',$2,$3,$4,$5)", [mention.id, mention.shop_id, keeper.id, note, now]); }
  for (const shop of losing) { await connection.execute("UPDATE shop_redirects SET target_shop_id=$1,reason='safe_duplicate_merge' WHERE target_shop_id=$2", [keeper.id, shop.id]); await connection.execute("INSERT INTO shop_redirects(source_shop_id,target_shop_id,reason,created_at) VALUES($1,$2,'safe_duplicate_merge',$3) ON CONFLICT(source_shop_id) DO UPDATE SET target_shop_id=excluded.target_shop_id,reason=excluded.reason", [shop.id, keeper.id, now]); }
  for (const shop of losing) await connection.execute('DELETE FROM shops WHERE id=$1', [shop.id]);
  return { ...result, action: 'merged', merged_shop_count: losing.length, moved_mention_count: moved.length, complemented_fields: fields };
}
export async function duplicateReport(database: Database, apply: boolean): Promise<DuplicateReport> {
  return database.transaction(async connection => {
    if (apply) await lockShopCreation(connection);
    const shops = await connection.query("SELECT s.* FROM shops s WHERE EXISTS(SELECT 1 FROM shop_mentions m WHERE m.shop_id=s.id AND m.review_status='approved') ORDER BY s.id" + (apply && connection.dialect === 'postgres' ? ' FOR UPDATE' : ''), shopRecord);
    if (apply && connection.dialect === 'postgres') await connection.query('SELECT source_shop_id,target_shop_id FROM shop_redirects ORDER BY source_shop_id FOR UPDATE', redirectRow);
    const groups = shadow(shops); const planned = await plan(connection, shops); const results: MergeResult[] = [];
    if (apply) for (const group of planned) results.push(await merge(connection, group, shops));
    return { groups, candidate_group_count: groups.length, candidate_shop_count: groups.reduce((sum, group) => sum + group.shop_ids.length, 0), auto_merge_group_count: groups.filter(group => group.would_auto_merge).length, safe_merge_plan: { groups: planned }, safe_merge_group_count: planned.filter(group => group.is_safe).length, safe_merge_shop_count: planned.filter(group => group.is_safe).reduce((sum, group) => sum + group.shop_ids.length, 0), safe_merge_result: apply ? { groups: results } : null, merged_group_count: results.filter(group => group.action === 'merged').length, merged_shop_count: results.reduce((sum, group) => sum + group.merged_shop_count, 0), moved_mention_count: results.reduce((sum, group) => sum + group.moved_mention_count, 0) };
  });
}
