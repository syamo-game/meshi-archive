import type { ExtractedMention } from './ai-models.js';
import { effectiveCandidateArea, matchesMentionNameAndArea, nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { canonicalArea } from './geography.js';
import { candidateIdentities, structuredSourceConflict } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { mentionRecord, shopRecord } from './records.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { areaIsMunicipality } from './region-master.js';
import { branchesConflict, evaluateIdentity, extractBranchToken, normalizeExternalIdentity, normalizeName, normalizeUrlIdentity } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { collectExternalIdentities, externalIdentity, findShopCollisions, identitiesConflict, lockShopCreation } from './shop-creation.js';
import { casefold, compareText } from './text.js';

type ShopMatch = readonly [ShopRecord | null, string | null];
function overlaps(left: ReadonlyMap<string, ReadonlySet<string>>, right: ReadonlyMap<string, ReadonlySet<string>>): boolean { return [...left].some(([source, ids]) => [...ids].some(id => right.get(source)?.has(id))); }
export function shopIdentities(shop: ShopRecord): ReadonlyMap<string, ReadonlySet<string>> { return collectExternalIdentities(shop.external_source, shop.external_id, [shop.canonical_url]); }
export function shopIdentity(shop: ShopRecord): CandidateIdentity { return { name: nameWithBranch(shop.shop_name, shop.branch_name), area: shop.area, category: shop.category, address: shop.address, phone: shop.phone, canonical_url: shop.canonical_url, external_source: shop.external_source, external_id: shop.external_id, evidence_url: shop.canonical_url }; }
export function preferredIdentity(candidate: CandidateIdentity): readonly [string | null, string | null] {
  const explicit = normalizeExternalIdentity(candidate.external_source, candidate.external_id); if (explicit) return explicit;
  for (const url of [candidate.canonical_url, candidate.evidence_url]) { const identity = externalIdentity(url); if (identity) { const normalized = normalizeExternalIdentity(...identity); if (normalized) return normalized; } }
  return [null, null];
}
function identityKey(name: string, branch: string | null, area: string | null): string | null {
  const normalized = normalizeName(nameWithBranch(name, branch)); const canonical = canonicalArea(area);
  return normalized && canonical && !areaIsMunicipality(canonical) ? JSON.stringify([normalized, normalizeName(branch || extractBranchToken(name) || ''), canonical]) : null;
}
export function mentionMatchesShop(mention: ExtractedMention, shop: ShopRecord, mentions: readonly MentionRecord[]): boolean {
  const key = identityKey(mention.shop_name, mention.branch_name, mention.area);
  return key !== null && (key === identityKey(shop.shop_name, shop.branch_name, shop.area) || mentions.some(row => row.shop_id === shop.id && row.review_status === 'approved' && key === identityKey(row.extracted_name, row.extracted_branch_name, row.extracted_area)));
}
export class PipelineShops {
  constructor(private readonly connection: Executor) {}
  private async data(): Promise<{ shops: ShopRecord[]; mentions: MentionRecord[]; approved: ReadonlySet<number> }> {
    const shops = await this.connection.query('SELECT * FROM shops ORDER BY id', shopRecord); const mentions = await this.connection.query('SELECT * FROM shop_mentions ORDER BY id', mentionRecord);
    return { shops, mentions, approved: new Set(mentions.filter(row => row.review_status === 'approved' && row.shop_id !== null).map(row => row.shop_id).filter((id): id is number => id !== null)) };
  }
  private evidenceMatches(shops: readonly ShopRecord[], candidate: CandidateIdentity): ShopRecord[] {
    const identities = candidateIdentities(candidate); const urls = new Set([candidate.canonical_url, candidate.evidence_url].map(normalizeUrlIdentity).filter(url => url !== null));
    return shops.filter(shop => overlaps(identities, shopIdentities(shop)) || urls.has(normalizeUrlIdentity(shop.canonical_url) ?? ''));
  }
  async fromMention(mention: ExtractedMention, canonicalHint: string | null): Promise<ShopMatch> {
    const { shops, mentions, approved } = await this.data(); const matches = (shop: ShopRecord): boolean => mentionMatchesShop(mention, shop, mentions);
    const posted = externalIdentity(canonicalHint);
    if (posted) {
      const [source, rawId] = posted; const id = casefold(rawId.trim());
      const all = shops.filter(shop => shopIdentities(shop).get(source)?.has(id));
      const exact = shops.filter(shop => approved.has(shop.id) && shopIdentities(shop).get(source)?.size === 1 && shopIdentities(shop).get(source)?.has(id) && matches(shop));
      if (all.length === 1 && exact.length === 1 && exact[0]) return [exact[0], '投稿URLの外部IDと店名・支店名・エリアが既存店舗と一致'];
      if (all.length > 1) return [null, '投稿URLの外部IDに複数の店舗が紐付いている'];
      if (all[0] && !approved.has(all[0].id)) return [null, '投稿URLの外部IDが未承認店舗と競合'];
    }
    const exact = shops.filter(matches);
    if (exact.some(shop => identitiesConflict(shopIdentities(shop)))) return [null, '店名・支店名・エリア一致の既存店舗内で外部IDが矛盾'];
    if (posted && exact.some(shop => { const ids = shopIdentities(shop).get(posted[0]); return ids && ids.size && !(ids.size === 1 && ids.has(casefold(posted[1].trim()))); })) return [null, '投稿URLの外部IDが店名・支店名・エリア一致の既存店舗と競合'];
    const confirmed = exact.filter(shop => approved.has(shop.id));
    if (confirmed.length === 1 && confirmed[0]) return [confirmed[0], '店名・支店名・エリアが承認済み既存店舗と一意に一致'];
    if (confirmed.length > 1) return [null, '店名・支店名・エリアが一致する承認済み店舗が複数ある'];
    return [null, exact.length ? '店名・支店名・エリアが未承認店舗と一致' : null];
  }
  async fromCandidates(mention: ExtractedMention, candidates: readonly PipelineCandidate[]): Promise<ShopMatch> {
    const { shops, mentions, approved } = await this.data(); const matches = new Map<number, ShopMatch>(); const conflicts = new Set<string>();
    for (const candidate of candidates) {
      if (!candidate.is_verified || !matchesMentionNameAndArea(mention, candidate)) continue;
      if (identitiesConflict(candidateIdentities(candidate))) return [null, '候補の外部IDが同一サービス内で矛盾'];
      if (candidate.provenance === 'structured_data' && structuredSourceConflict(mention, candidate, candidates)) return [null, '同じ構造化データ出典の店舗候補が矛盾'];
      const evidence = this.evidenceMatches(shops, candidate);
      if (evidence.length > 1) return [null, '候補の外部IDまたはURLが複数の既存店舗に紐付いている'];
      if (evidence[0]) {
        if (identitiesConflict(shopIdentities(evidence[0]))) return [null, '候補の根拠に一致する既存店舗内で外部IDが矛盾'];
        if (!approved.has(evidence[0].id)) return [null, '候補の外部IDまたはURLが未承認店舗と競合'];
        if (!mentionMatchesShop(mention, evidence[0], mentions)) continue;
      }
      for (const shop of shops) {
        if (!approved.has(shop.id) || !mentionMatchesShop(mention, shop, mentions)) continue;
        if (identitiesConflict(shopIdentities(shop))) { conflicts.add('external_id'); continue; }
        const outcome = evaluateIdentity(shopIdentity(shop), candidate);
        if (outcome.is_strong_match && outcome.conflicting_fields.length) for (const field of outcome.conflicting_fields) conflicts.add(field);
        else if (outcome.is_strong_match) matches.set(shop.id, [shop, outcome.reason]);
      }
    }
    if (conflicts.size) return [null, '強い一致根拠と候補情報が矛盾: fields=' + [...conflicts].sort(compareText).join(',')];
    if (matches.size === 1) { const match = [...matches.values()][0]; if (match) return match; }
    return [null, matches.size > 1 ? '複数の既存店舗が強い一致条件を満たしたため自動統合しない' : null];
  }
  async afterConfirmation(mention: ExtractedMention, candidate: PipelineCandidate): Promise<ShopMatch> {
    const updated = { ...mention, area: effectiveCandidateArea(mention, candidate) || mention.area, category: candidate.category || mention.category };
    if (identitiesConflict(candidateIdentities(candidate))) return [null, '候補の外部IDが同一サービス内で矛盾'];
    const { shops, mentions, approved } = await this.data(); const evidence = this.evidenceMatches(shops, candidate);
    if (evidence.some(shop => identitiesConflict(shopIdentities(shop)))) return [null, '候補の根拠に一致する既存店舗内で外部IDが矛盾'];
    const matches = evidence.filter(shop => approved.has(shop.id) && mentionMatchesShop(updated, shop, mentions));
    if (evidence.length === 1 && matches.length === 1 && matches[0]) return [matches[0], '候補の外部IDまたはURLと店名・支店名・エリアが既存店舗と一致'];
    if (evidence.length > 1) return [null, '候補の外部IDまたはURLが複数の既存店舗に紐付いている'];
    return evidence.length ? [null, '候補の外部IDまたはURLが未承認店舗または異なる店舗情報と競合'] : this.fromMention(updated, null);
  }
  async guardCreation(mention: ExtractedMention, candidate: PipelineCandidate | null, blocked: string | null): Promise<readonly [PipelineCandidate | null, ShopRecord | null, string | null]> {
    if (blocked !== null || candidate === null) return [null, null, blocked];
    await lockShopCreation(this.connection);
    const identities = candidateIdentities(candidate);
    if (identitiesConflict(identities)) return [null, null, '新規候補の外部IDが同一サービス内で矛盾'];
    const area = canonicalArea(candidate.area) || canonicalArea(mention.area); const [source, id] = preferredIdentity(candidate);
    const collisions = await findShopCollisions(this.connection, { shop_name: candidate.name, branch_name: null, area, address: candidate.address, phone: candidate.phone, canonical_url: candidate.canonical_url, evidence_url: candidate.evidence_url, external_source: source, external_id: id });
    const { shops, mentions, approved } = await this.data();
    if (collisions.length) {
      const ids = new Set(collisions.map(collision => collision.shop_id));
      if (ids.size !== 1) return [null, null, '検証済み候補が複数の既存店舗と競合'];
      const shopId = collisions[0]?.shop_id; const shop = shops.find(item => item.id === shopId);
      if (!shop) return [null, null, `衝突先の既存店舗が見つからない: shop_id=${shopId}`];
      const reason = (text: string): readonly [null, null, string] => [null, null, `${text}: shop_id=${shop.id}`];
      if (!approved.has(shop.id)) return reason('検証済み候補が未承認店舗と競合');
      if (!candidate.is_verified) return reason('未検証候補が既存店舗と競合');
      if (!collisions.some(collision => collision.matched_by !== 'evidence_url')) return reason('根拠URLだけが既存店舗と一致');
      const existing = shopIdentities(shop);
      if (identitiesConflict(existing)) return reason('既存店舗内で外部IDが矛盾');
      for (const [service, candidateIds] of identities) { const existingIds = existing.get(service); if (existingIds?.size && ![...candidateIds].some(value => existingIds.has(value))) return [null, null, `同一サービスの外部IDが既存店舗と矛盾: shop_id=${shop.id}; source=${service}`]; }
      const outcome = evaluateIdentity(shopIdentity(shop), candidate);
      if (outcome.conflicting_fields.length) return [null, null, `検証済み候補と既存店舗が矛盾: shop_id=${shop.id}; fields=${outcome.conflicting_fields.join(',')}`];
      const existingArea = canonicalArea(shop.area); if (existingArea && area && existingArea !== area) return reason('検証済み候補と既存店舗のエリアが矛盾');
      const branch = normalizeName(mention.branch_name || extractBranchToken(mention.shop_name) || ''); const existingBranch = normalizeName(shop.branch_name || extractBranchToken(shop.shop_name) || '');
      if (branchesConflict(nameWithBranch(mention.shop_name, mention.branch_name), nameWithBranch(shop.shop_name, shop.branch_name)) || (collisions.some(collision => collision.matched_by === 'name_area') && Boolean(branch) !== Boolean(existingBranch) && !overlaps(identities, existing))) return reason('検証済み候補と既存店舗の支店名が矛盾');
      return [null, shop, `検証済み候補の衝突先が承認済み既存店舗一件: shop_id=${shop.id}`];
    }
    if (shops.some(shop => evaluateIdentity(shopIdentity(shop), candidate).is_strong_match)) return [null, null, '新規候補の電話・住所が既存店舗と強く一致'];
    if (shops.some(shop => mentionMatchesShop(mention, shop, mentions))) return [null, null, '新規候補の店名・支店名・エリアが既存店舗と競合'];
    return [candidate, null, null];
  }
}

