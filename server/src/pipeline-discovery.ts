import { createHash } from 'node:crypto';
import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import { ExtractionError, RestaurantAI } from './ai.js';
import { extractedMessageSchema, requireExtractionEvidence } from './ai-models.js';
import type { ExtractedMention, ExtractedMessage } from './ai-models.js';
import { candidateAddressPreciselyMatchesArea, canonicalAreasInText, nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { canonicalArea } from './geography.js';
import { fetchHtmlDocument, HtmlFetchError } from './html-fetch.js';
import type { MessageEnvelope } from './ingestion.js';
import { safeHttpUrl } from './links.js';
import { extractRestaurantPageEvidence, pageMentionsRestaurantName, pageTextWithoutRestaurantName } from './page-evidence.js';
import { PipelineJournal } from './pipeline-journal.js';
import { normalizeName } from './resolution.js';
import { citationUrlIdentity } from './source-evidence.js';

export interface DiscoveryEvidence { input_text: string; cache_value: string; source_urls: readonly string[]; input_source_urls: readonly string[]; documents: readonly { url: string; metadata_blocks: readonly string[] }[]; input_truncated: boolean; }
const proofSchema = z.strictObject({ final_url: z.string().min(1).max(2048), page_sha256: z.string().regex(/^[0-9a-f]{64}$/), normalized_name: z.string().min(1).max(500), canonical_area: z.string().min(1).max(255) });
const discoverySchema = z.strictObject({ message: extractedMessageSchema, source_urls: z.array(z.string()) });
const truncate = (value: string, maximum: number): string => [...value].slice(0, maximum).join('');
export function extractHttpUrls(text: string): readonly string[] {
  const urls = new Set<string>();
  for (const match of text.matchAll(/https?:\/\/[^\s<>"']+/gi)) {
    const url = match[0].replace(/[.,;:!?\)\]\}、。）」』，；：！？］｝”’】〉》〕〗〙〛]+$/u, '');
    if (safeHttpUrl(url)) urls.add(url); else console.warn('Invalid plain-text URL excluded', { offset: match.index });
  }
  return [...urls];
}
export function discoveryEvidence(envelope: MessageEnvelope): DiscoveryEvidence | null {
  const byUrl = new Map<string, MessageEnvelope['assets'][number]>();
  for (const asset of envelope.assets) {
    if (asset.kind !== 'link' && asset.kind !== 'embed') continue;
    const old = byUrl.get(asset.url);
    if (!old || (asset.kind === 'embed' && (asset.title || asset.description) && !(old.title || old.description))) byUrl.set(asset.url, asset);
  }
  const all = [...byUrl.values()]; const selected = all.slice(0, 3); if (!selected.length) return null;
  const parts: string[] = []; const documents: { url: string; metadata_blocks: string[] }[] = [];
  for (const asset of selected) {
    parts.push('[Source URL] ' + asset.url); const blocks: string[] = [];
    if (asset.title) { const title = truncate(asset.title, 2000); parts.push('[Source Title] ' + title); blocks.push(title); }
    if (asset.description) { const description = truncate(asset.description, 8000); parts.push('[Source Description] ' + description); blocks.push(description); }
    documents.push({ url: asset.url, metadata_blocks: blocks });
  }
  parts.push('[Discord Message]\n' + envelope.content); const full = parts.join('\n'); const input = truncate(full, 60000);
  const digest = createHash('sha256').update(input.replace(/\s+/gu, ' ').trim()).digest('hex');
  const urls = [...new Set([...all.map(asset => asset.url), ...extractHttpUrls(envelope.content), ...all.flatMap(asset => [asset.title, asset.description].filter((text): text is string => Boolean(text)).flatMap(extractHttpUrls))])];
  return { input_text: input, cache_value: `${contract.versions.discovery}:${digest}`, source_urls: urls, input_source_urls: selected.map(asset => asset.url), documents, input_truncated: all.length > selected.length || [...full].length > 60000 || selected.some(asset => [...(asset.description ?? '')].length > 8000) };
}
export function sourceBlockMatches(name: string, area: string, block: string): boolean {
  const withoutName = pageTextWithoutRestaurantName(name, block); if (withoutName === null) return false;
  const areas = canonicalAreasInText(withoutName); return areas.size === 1 && areas.has(area);
}
export class SourceDiscovery {
  constructor(private readonly connection: Executor, private readonly ai: RestaurantAI, private readonly journal: PipelineJournal, private readonly messageId: string) {}
  async grounded(mention: ExtractedMention, evidence: DiscoveryEvidence, sources: readonly string[]): Promise<readonly [boolean, string | null]> {
    if (evidence.input_truncated) return [false, '検索入力で省略された本文・出典があり、根拠の全体を確認できない'];
    if (!mention.source_url) return [false, '店舗ごとの元出典URLがない'];
    const identity = citationUrlIdentity(mention.source_url);
    if (!evidence.input_source_urls.some(url => citationUrlIdentity(url) === identity)) return [false, '店舗の元出典URLが検索入力に含まれない'];
    if (!sources.some(url => citationUrlIdentity(url) === identity)) return [false, '店舗の元出典URLがWeb検索の引用元に含まれない'];
    const source = evidence.documents.find(document => citationUrlIdentity(document.url) === identity);
    if (!source) return [false, '店舗の元出典を入力資料へ対応付けられない'];
    const name = nameWithBranch(mention.shop_name, mention.branch_name); const normalized = normalizeName(name); const area = canonicalArea(mention.area);
    if (!area) return [false, '店舗の地域をアプリの粒度へ正規化できない'];
    if (normalized && source.metadata_blocks.some(block => sourceBlockMatches(name, area, block))) return [true, null];
    const kind = 'source_mention_evidence_v2'; const key = `${identity}|${normalized}|${normalizeName(area)}`; const cached = await this.journal.get(this.connection, kind, key);
    if (cached !== null) {
      try { const proof = proofSchema.parse(JSON.parse(cached)); if (proof.normalized_name === normalized && proof.canonical_area === area && citationUrlIdentity(proof.final_url) === identity) return [true, null]; }
      catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; this.journal.failure(this.messageId, 'source_mention_evidence_cache', new ExtractionError(`url=${source.url}; invalid cached payload: ${error.message}`)); }
      this.journal.invalidate(kind, key);
    }
    const document = await fetchHtmlDocument(source.url).catch(error => { if (!(error instanceof HtmlFetchError)) throw error; this.journal.failure(this.messageId, 'source_mention_evidence', new ExtractionError(`url=${source.url}; shop=${mention.shop_name}; ${error.name}: ${error.message}`)); return null; });
    if (!document) return [false, '元出典ページをサーバーから取得できない'];
    if (citationUrlIdentity(document.final_url) !== identity) return [false, '元出典ページの転送先が検索入力URLと一致しない'];
    const page = extractRestaurantPageEvidence(document.html, document.final_url); const named = page.structured_candidates.filter(candidate => normalizeName(candidate.name) === normalized);
    const hasArea = named.length ? named.length === 1 && Boolean(named[0]?.address && candidateAddressPreciselyMatchesArea(area, named[0].address)) : page.text_blocks.some(block => sourceBlockMatches(name, area, block));
    if (!pageMentionsRestaurantName(name, page) || !hasArea) return [false, '元出典ページ上で店舗名と地域を同時に確認できない'];
    this.journal.put(kind, key, JSON.stringify({ final_url: document.final_url, page_sha256: page.page_sha256, normalized_name: normalized, canonical_area: area }));
    return [true, null];
  }
  async discover(evidence: DiscoveryEvidence): Promise<ExtractedMessage> {
    const cached = await this.journal.get(this.connection, 'source_discovery', evidence.cache_value); let result: z.infer<typeof discoverySchema> | null = null;
    if (cached !== null) {
      try { result = discoverySchema.parse(JSON.parse(cached)); }
      catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; this.journal.failure(this.messageId, 'source_discovery_cache', new ExtractionError(`source_digest=${evidence.cache_value}; invalid cached payload: ${error.message}`)); this.journal.invalidate('source_discovery', evidence.cache_value); }
    }
    if (!result) {
      const response = await this.ai.discover(evidence.input_text).catch(error => { if (error instanceof Error) this.journal.failure(this.messageId, 'source_discovery', error); throw error; });
      this.journal.metrics(this.messageId, 'source_discovery', response.metrics);
      if (response.metrics.web_search_calls !== response.metrics.api_attempts) { const error = new ExtractionError(`Source discovery must use one web search per API attempt: message_id=${this.messageId}; calls=${response.metrics.web_search_calls}; attempts=${response.metrics.api_attempts}`); this.journal.failure(this.messageId, 'source_discovery_validation', error); throw error; }
      result = { message: response.message, source_urls: [...response.source_urls] }; this.journal.put('source_discovery', evidence.cache_value, JSON.stringify(result));
    }
    const message = requireExtractionEvidence(result.message); const mentions: ExtractedMention[] = []; let failed = false;
    for (const mention of message.mentions) {
      const [grounded, reason] = await this.grounded(mention, evidence, result.source_urls);
      if (grounded) { mentions.push(mention); continue; }
      const explanation = reason || '店舗ごとの元出典を確認できない'; failed = true;
      mentions.push({ ...mention, source_url: null, needs_review: true, confidence_reason: truncate(`${mention.confidence_reason} / 元出典未確認: ${explanation}`, 2000) });
      this.journal.failure(this.messageId, 'source_discovery_grounding', new ExtractionError(`message_id=${this.messageId}; shop=${mention.shop_name}; reason=${explanation}`));
    }
    return failed ? { ...message, mentions, unresolved_reason: null } : message;
  }
}
