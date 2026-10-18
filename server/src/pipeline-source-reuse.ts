import type { Executor } from './db/database.js';
import { inputEvidenceAssessment, sourceRequiresFreshExtraction } from './extraction-safety.js';
import type { MessageEnvelope } from './ingestion.js';
import { candidateIdentities } from './pipeline-candidates.js';
import { shopIdentities, shopIdentity } from './pipeline-shops.js';
import { candidateRecord, mentionRecord, shopRecord } from './records.js';
import type { MentionRecord, ShopRecord } from './records.js';
import { evaluateIdentity, normalizeName } from './resolution.js';
import { identitiesConflict } from './shop-creation.js';
import { identifySourceUrl } from './source-identity.js';
import type { SourceIdentity } from './source-identity.js';

export interface SourceReuseMatch { shop: ShopRecord; mention: MentionRecord; identity: SourceIdentity; }
export function sourceIdentityForReuse(envelope: MessageEnvelope): SourceIdentity | null {
  if (inputEvidenceAssessment(envelope.content, Math.max(envelope.omitted_asset_count, envelope.assets.length - 50)).codes.length || sourceRequiresFreshExtraction(envelope.content)) return null;
  const identities = new Map<string, SourceIdentity>();
  for (const asset of envelope.assets) {
    if (asset.kind === 'image' && asset.is_embed_preview) continue;
    if (asset.kind !== 'link' && asset.kind !== 'embed') return null;
    const identity = identifySourceUrl(asset.url); if (!identity) return null;
    identities.set(JSON.stringify([identity.service, identity.item_id, identity.normalized_url]), identity);
  }
  if (identities.size !== 1) return null;
  for (const line of envelope.content.split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/u)) {
    const stripped = line.trim(); if (!stripped || ['[Embed URL]', '[Embed Title]', '[Embed Description]', '[Embed Field:'].some(prefix => stripped.startsWith(prefix))) continue;
    if (normalizeName(stripped.replace(/https?:\/\/[^\s<>]+/g, ''))) return null;
  }
  return [...identities.values()][0] ?? null;
}
export async function findSourceReuse(connection: Executor, envelope: MessageEnvelope): Promise<SourceReuseMatch | null> {
  const identity = sourceIdentityForReuse(envelope); if (!identity) return null;
  const rows = await connection.query('SELECT m.* FROM shop_mentions m WHERE m.message_id IN (SELECT DISTINCT a.message_id FROM source_assets a WHERE a.source_service=$1 AND a.source_item_id=$2 AND a.normalized_url=$3 AND a.message_id<>$4) ORDER BY m.id', mentionRecord, [identity.service, identity.item_id, identity.normalized_url, envelope.message_id]);
  const byMessage = new Map<string, MentionRecord[]>();
  for (const row of rows) { const group = byMessage.get(row.message_id) ?? []; group.push(row); byMessage.set(row.message_id, group); }
  const reusable: MentionRecord[] = [];
  for (const group of byMessage.values()) {
    const active = group.filter(row => row.review_status !== 'rejected'); if (!active.length) continue;
    const mention = active[0];
    if (active.length !== 1 || !mention || mention.review_status !== 'approved' || mention.shop_id === null || mention.extraction_error?.startsWith('evidence_review:')) return null;
    reusable.push(mention);
  }
  const ids = new Set(reusable.map(row => row.shop_id)); if (ids.size !== 1) return null;
  const last = reusable.sort((a, b) => a.id - b.id).at(-1); if (!last || last.shop_id === null) return null;
  const shop = (await connection.query('SELECT * FROM shops WHERE id=$1', shopRecord, [last.shop_id]))[0]; if (!shop) return null;
  const identities = new Map([...shopIdentities(shop)].map(([source, values]) => [source, new Set(values)]));
  for (const mention of reusable) {
    const candidates = await connection.query('SELECT * FROM resolution_candidates WHERE mention_id=$1 AND is_strong_match=$2', candidateRecord, [mention.id, true]);
    for (const candidate of candidates) {
      if (evaluateIdentity(shopIdentity(shop), candidate).conflicting_fields.length) return null;
      for (const [source, values] of candidateIdentities(candidate)) { const idsForSource = identities.get(source) ?? new Set<string>(); for (const value of values) idsForSource.add(value); identities.set(source, idsForSource); }
    }
  }
  return identitiesConflict(identities) ? null : { shop, mention: last, identity };
}
