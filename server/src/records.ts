import { z } from 'zod';
import { booleanColumn, timestampColumn } from './db/database.js';

export const shopRecord = z.object({ id: z.number().int(), version: z.number().int(), shop_name: z.string(), branch_name: z.string().nullable(), area: z.string().nullable(), category: z.string().nullable(), address: z.string().nullable(), phone: z.string().nullable(), canonical_url: z.string().nullable(), is_visited: booleanColumn, visited_at: timestampColumn.nullable(), rating: z.number().nullable(), memo: z.string().nullable(), external_source: z.string().nullable(), external_id: z.string().nullable(), image_key: z.string().nullable(), created_at: timestampColumn, updated_at: timestampColumn });
export type ShopRecord = z.infer<typeof shopRecord>;
export const mentionRecord = z.object({ id: z.number().int(), shop_id: z.number().int().nullable(), version: z.number().int(), message_id: z.string(), occurrence_index: z.number().int(), extracted_name: z.string(), extracted_branch_name: z.string().nullable(), extracted_area: z.string().nullable(), extracted_category: z.string().nullable(), review_status: z.enum(['pending', 'approved', 'rejected', 'deferred']), resolution_status: z.enum(['resolved', 'ambiguous', 'not_found', 'invalid']), metadata_review_status: z.enum(['pending', 'approved', 'deferred']), resolution_method: z.enum(['automatic', 'manual']).nullable(), difference_type: z.string().nullable(), metadata_difference_type: z.string().nullable(), reviewed_at: timestampColumn.nullable(), metadata_reviewed_at: timestampColumn.nullable(), source_url: z.string().nullable(), extraction_source: z.string(), extraction_error: z.string().nullable(), confidence_reason: z.string().nullable(), reused_from_mention_id: z.number().int().nullable(), resolution_basis: z.string().nullable(), created_at: timestampColumn });
export type MentionRecord = z.infer<typeof mentionRecord>;
export const candidateRecord = z.object({ id: z.number().int(), mention_id: z.number().int(), rank: z.number().int(), name: z.string(), area: z.string().nullable(), category: z.string().nullable(), address: z.string().nullable(), phone: z.string().nullable(), canonical_url: z.string().nullable(), external_source: z.string().nullable(), external_id: z.string().nullable(), evidence_url: z.string().nullable(), provenance: z.enum(['posted_url', 'structured_data', 'web_search', 'image']), is_verified: booleanColumn, verification_reason: z.string().nullable(), matched_fields: z.string().nullable(), conflicting_fields: z.string().nullable(), name_similarity_milli: z.number().int(), is_strong_match: booleanColumn, created_at: timestampColumn });
export type CandidateRecord = z.infer<typeof candidateRecord>;
export const messageRecord = z.object({ message_id: z.string(), channel_id: z.string().nullable(), content: z.string().nullable(), source_created_at: timestampColumn.nullable(), is_target: booleanColumn, processing_status: z.enum(['pending', 'processing', 'succeeded', 'failed', 'ignored']), fetch_error: z.string().nullable(), processed_at: timestampColumn.nullable() });
export type MessageRecord = z.infer<typeof messageRecord>;
export const assetRecord = z.object({ id: z.number().int(), message_id: z.string(), kind: z.enum(['link', 'embed', 'image', 'attachment']), url: z.string(), title: z.string().nullable(), description: z.string().nullable(), mime_type: z.string().nullable(), extracted_text: z.string().nullable(), source_service: z.string().nullable(), source_item_id: z.string().nullable(), normalized_url: z.string().nullable(), content_fingerprint: z.string().nullable(), fetch_status: z.enum(['pending', 'available', 'unavailable']), fetch_error: z.string().nullable(), created_at: timestampColumn });
export type AssetRecord = z.infer<typeof assetRecord>;
export const reviewEventRecord = z.object({ id: z.number().int(), mention_id: z.number().int(), scope: z.enum(['identity', 'metadata']), action: z.string(), previous_shop_id: z.number().nullable(), selected_shop_id: z.number().nullable(), note: z.string().nullable(), created_at: timestampColumn });
export type ReviewEventRecord = z.infer<typeof reviewEventRecord>;

export function primaryMention(mentions: readonly MentionRecord[]): MentionRecord | undefined {
  const approved = mentions.filter(mention => mention.review_status === 'approved'); const candidates = approved.length ? approved : mentions;
  const first = candidates[0];
  return first?.extraction_source === 'legacy_import' && first.source_url ? candidates.find(mention => mention.source_url === first.source_url && Boolean(mention.extraction_source) && mention.extraction_source !== 'legacy_import') ?? first : first;
}
export function eventExcluded(mention: MentionRecord): boolean { return mention.difference_type === 'event_excluded' && mention.shop_id === null && mention.review_status === 'rejected'; }
export function metadataEligible(mention: MentionRecord): boolean { return !(mention.review_status === 'rejected' && mention.difference_type === 'manual_excluded') && !eventExcluded(mention); }
export function statusMatches(mention: MentionRecord, status: MentionRecord['review_status'], scope: 'all' | 'identity' | 'metadata'): boolean {
  if (scope === 'identity') return mention.review_status === status;
  if (scope === 'metadata') return metadataEligible(mention) && mention.metadata_review_status === status;
  if (status === 'approved') return mention.review_status === status && (mention.metadata_review_status === status || !metadataEligible(mention));
  if (status === 'rejected') return mention.review_status === status;
  return mention.review_status === status || (mention.review_status === 'approved' && metadataEligible(mention) && mention.metadata_review_status === status);
}
export function openReview(mention: MentionRecord): boolean { return statusMatches(mention, 'pending', 'all') || statusMatches(mention, 'deferred', 'all'); }
