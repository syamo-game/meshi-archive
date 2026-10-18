import { z } from 'zod';
import { safeHttpUrl } from './links.js';
import { identitySchema } from './resolution.js';

export const subjectKind = z.enum(['restaurant', 'product', 'event', 'unknown']);
export const identityEvidence = z.enum(['explicit', 'ambiguous', 'author_only', 'unknown']);
export const operatingStatus = z.enum(['open', 'closed', 'event_ended', 'unknown']);
const text = (maximum: number): z.ZodDefault<z.ZodNullable<z.ZodString>> => z.string().trim().max(maximum).nullable().default(null);
const optionalText = (maximum: number) => text(maximum).transform(value => value || null);
export const eventOriginSchema = z.strictObject({ shop_name: z.string().trim().min(1).max(500), branch_name: text(255), area: text(255), is_unique: z.boolean().default(false), relation_evidence: z.string().trim().min(1).max(2000), permanent_evidence: z.string().trim().min(1).max(2000), relation_source_url: text(2048), permanent_source_url: text(2048) });
export const extractedMentionSchema = z.strictObject({ shop_name: z.string().trim().min(1).max(500), branch_name: optionalText(255), area: optionalText(255), category: optionalText(255), source_url: optionalText(2048).refine(value => value === null || safeHttpUrl(value) !== null, 'Source URL must use HTTP or HTTPS without credentials'), needs_review: z.boolean(), confidence_reason: z.string().trim().min(1).max(2000), subject_kind: subjectKind.nullable().default(null), identity_evidence: identityEvidence.nullable().default(null), name_evidence: text(2000), branch_evidence: text(2000), operating_status: operatingStatus.nullable().default(null), operating_status_evidence: text(2000), event_origin: eventOriginSchema.nullable().default(null) });
export type ExtractedMention = z.infer<typeof extractedMentionSchema>;
export const extractedMessageSchema = z.strictObject({ is_restaurant_message: z.boolean(), ignore_reason: text(2000), unresolved_reason: text(2000), mentions: z.array(extractedMentionSchema).max(30) }).refine(value => value.is_restaurant_message || (!value.mentions.length && !value.unresolved_reason), 'Ignored messages cannot have mentions or an unresolved reason');
export type ExtractedMessage = z.infer<typeof extractedMessageSchema>;
export const searchCandidateSchema = identitySchema.extend({ confidence_reason: z.string().trim().min(1).max(2000) });
export type SearchCandidate = z.infer<typeof searchCandidateSchema>;
export const searchCandidateSetSchema = z.strictObject({ candidates: z.array(searchCandidateSchema).max(5), unresolved_reason: text(2000) });
export type SearchCandidateSet = z.infer<typeof searchCandidateSetSchema>;
export const imageCluesSchema = z.strictObject({ usable: z.boolean(), image_type: z.string().trim().min(1).max(64), visible_shop_names: z.array(z.string().trim()).max(10), address_clues: z.array(z.string().trim()).max(10), phone_clues: z.array(z.string().trim()).max(10), reason: z.string().trim().min(1).max(2000), subject_kind: subjectKind.default('unknown'), operating_status: operatingStatus.default('unknown'), operating_status_evidence: text(2000), event_origin: eventOriginSchema.nullable().default(null) });
export type ImageClues = z.infer<typeof imageCluesSchema>;
export const candidateSearchContextSchema = z.strictObject({ category: text(255), address: text(2000), phone: text(64), supplement: z.string().trim().max(4000).default(''), reference_urls: z.array(z.string()).max(2).default([]), image_inputs: z.array(z.string()).max(2).default([]) });
export type CandidateSearchContext = z.infer<typeof candidateSearchContextSchema>;
export interface ModelCallMetrics { model: string; input_tokens: number; output_tokens: number; web_search_calls: number; image_count: number; latency_ms: number; estimated_cost_microusd: number; api_attempts: number; }
export interface ExtractionCallResult { message: ExtractedMessage; metrics: ModelCallMetrics; source_urls: readonly string[]; }
export interface CandidateSearchResult { candidates: SearchCandidateSet; metrics: ModelCallMetrics; source_urls: readonly string[]; }
export interface ImageAnalysisResult { clues: ImageClues; metrics: ModelCallMetrics; }
export function requireExtractionEvidence(message: ExtractedMessage): ExtractedMessage {
  return { ...message, mentions: message.mentions.map(mention => mention.identity_evidence === null || mention.subject_kind === null ? { ...mention, identity_evidence: mention.identity_evidence ?? 'unknown', subject_kind: mention.subject_kind ?? 'unknown', needs_review: true } : mention) };
}
