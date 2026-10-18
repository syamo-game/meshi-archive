import { z } from 'zod';
import contract from '../data/ai-contract.json' with { type: 'json' };
import { ExtractionError, RestaurantAI } from './ai.js';
import { searchCandidateSetSchema } from './ai-models.js';
import type { ExtractedMention, SearchCandidateSet } from './ai-models.js';
import { effectiveCandidateArea, matchesMentionNameAndArea, nameWithBranch } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { fetchHtmlDocument, HtmlFetchError } from './html-fetch.js';
import { extractJsonLdCandidates, extractRestaurantPageEvidence, provePageCandidate } from './page-evidence.js';
import { dedupeCandidates, pageCandidateCacheValue, pageVerifiedCandidateValid, pipelineCandidateSchema, topWebPolicy, unresolvedRedirect, verifyFromStructuredPage } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import { PipelineJournal } from './pipeline-journal.js';
import { identitySchema, nameSimilarity, normalizeArea, normalizeName } from './resolution.js';
import type { CandidateIdentity } from './resolution.js';
import { citationUrlIdentity, sanitizeCitedCandidate, sourceEvidenceUrlIdentity } from './source-evidence.js';

export interface CandidateBatch { candidates: readonly PipelineCandidate[]; unresolved_reason: string | null; }
const cachedPageProof = z.strictObject({ final_url: z.string().min(1).max(2048), method: z.enum(['structured_data', 'visible_page']), page_sha256: z.string().regex(/^[0-9a-f]{64}$/), candidate: pipelineCandidateSchema });
export class PipelineEvidence {
  constructor(private readonly connection: Executor, private readonly ai: RestaurantAI, private readonly journal: PipelineJournal, private readonly messageId: string) {}
  private invalidCache(stage: string, kind: string, key: string, error: Error): void { this.journal.failure(this.messageId, stage, new ExtractionError(`key=${key}; invalid cached payload: ${error.message}`)); this.journal.invalidate(kind, key); }
  async structured(url: string): Promise<PipelineCandidate[]> {
    const cached = await this.journal.get(this.connection, 'url_metadata', url);
    const convert = (candidate: CandidateIdentity): PipelineCandidate => ({ ...candidate, provenance: 'structured_data', is_verified: true, verification_reason: `サーバーが取得した構造化データ: ${url}` });
    if (cached !== null) {
      try { return z.strictObject({ candidates: z.array(identitySchema) }).parse(JSON.parse(cached)).candidates.map(convert); }
      catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; this.invalidCache('url_metadata_cache', 'url_metadata', url, error); }
    }
    let fetched: readonly CandidateIdentity[];
    try { const document = await fetchHtmlDocument(url); fetched = extractJsonLdCandidates(document.html, document.final_url); }
    catch (error) {
      if (!(error instanceof HtmlFetchError || error instanceof z.ZodError)) throw error;
      this.journal.failure(this.messageId, 'url_metadata', new ExtractionError(`url=${url}; ${error.name}: ${error.message}`)); return [];
    }
    const candidates = dedupeCandidates(fetched.map(convert));
    const identities = candidates.map(({ provenance: _provenance, is_verified: _verified, verification_reason: _reason, ...identity }) => identity);
    this.journal.put('url_metadata', url, JSON.stringify({ candidates: identities }));
    return candidates;
  }
  async web(mention: ExtractedMention): Promise<CandidateBatch> {
    const key = [contract.versions.search, normalizeName(mention.shop_name), normalizeName(mention.branch_name ?? ''), normalizeArea(mention.area)].join('|');
    const cached = await this.journal.get(this.connection, 'web_search', key); let candidateSet: SearchCandidateSet | null = null;
    if (cached !== null) {
      try { candidateSet = searchCandidateSetSchema.parse(JSON.parse(cached)); }
      catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; this.invalidCache('candidate_search_cache', 'web_search', key, error); }
    }
    if (candidateSet === null) {
      const result = await this.ai.search(mention).catch(error => { if (error instanceof Error) this.journal.failure(this.messageId, 'candidate_search', error); throw error; });
      this.journal.metrics(this.messageId, 'candidate_search', result.metrics);
      if (result.metrics.web_search_calls !== result.metrics.api_attempts) {
        const error = new ExtractionError(`Candidate search must use one web search per API attempt: message_id=${this.messageId}; calls=${result.metrics.web_search_calls}; attempts=${result.metrics.api_attempts}`);
        this.journal.failure(this.messageId, 'candidate_search_validation', error); throw error;
      }
      const candidates = result.candidates.candidates.map(candidate => sanitizeCitedCandidate(candidate, result.source_urls)).filter(candidate => candidate !== null);
      let reason = result.candidates.unresolved_reason;
      if (candidates.length !== result.candidates.candidates.length) reason = [reason, '検索元に含まれない候補URLを除外しました。'].filter(Boolean).join(' / ');
      candidateSet = { candidates, unresolved_reason: reason };
      this.journal.put('web_search', key, JSON.stringify(candidateSet));
    }
    return { candidates: candidateSet.candidates.map(({ confidence_reason: _reason, ...candidate }) => ({ ...candidate, provenance: 'web_search', is_verified: false, verification_reason: '候補URLは検索元と一致、店舗情報はサーバー検証前' })), unresolved_reason: candidateSet.unresolved_reason };
  }
  async verifyPage(mention: ExtractedMention, candidate: PipelineCandidate, candidates: readonly PipelineCandidate[], excludedUrls: readonly string[] = []): Promise<readonly [PipelineCandidate | null, string | null]> {
    const structured = verifyFromStructuredPage(mention, candidate, candidates); if (structured) return [structured, null];
    const url = candidate.canonical_url || candidate.evidence_url;
    if (!url) return [null, 'Web候補に引用済み店舗URLがない'];
    if (unresolvedRedirect([url])) return [null, '短縮URLの転送先を店舗根拠として確認できない'];
    const kind = 'page_evidence_v2'; const key = pageCandidateCacheValue(mention, candidate, url);
    const excluded = new Set(excludedUrls.map(sourceEvidenceUrlIdentity));
    const cached = await this.journal.get(this.connection, kind, key);
    if (cached !== null) {
      try {
        const proof = cachedPageProof.parse(JSON.parse(cached));
        if (citationUrlIdentity(proof.final_url) === citationUrlIdentity(url) && !excluded.has(sourceEvidenceUrlIdentity(proof.final_url)) && pageVerifiedCandidateValid(mention, proof.candidate)) return [proof.candidate, null];
        this.journal.invalidate(kind, key);
      } catch (error) { if (!(error instanceof SyntaxError || error instanceof z.ZodError)) throw error; this.invalidCache('page_evidence_cache', kind, key, error); }
    }
    const document = await fetchHtmlDocument(url).catch(error => {
      if (!(error instanceof HtmlFetchError)) throw error;
      this.journal.failure(this.messageId, 'page_evidence', new ExtractionError(`url=${url}; ${error.name}: ${error.message}`)); return null;
    });
    if (!document) return [null, '候補ページをサーバーから取得できない'];
    if (citationUrlIdentity(document.final_url) !== citationUrlIdentity(url)) return [null, '候補ページの転送先が検索で引用された店舗URLと一致しない'];
    if (excluded.has(sourceEvidenceUrlIdentity(document.final_url))) return [null, '候補ページが元投稿の出典へ戻るため独立根拠にならない'];
    const proof = provePageCandidate(candidate, extractRestaurantPageEvidence(document.html, document.final_url));
    if (!proof) return [null, '同一ページ上で店舗名と完全な住所を確認できない'];
    const area = effectiveCandidateArea(mention, proof.candidate);
    if (!area) return [null, '候補ページの住所から管理可能な地域を一意に特定できない'];
    const verified: PipelineCandidate = { ...proof.candidate, area, evidence_url: document.final_url, provenance: 'web_search', is_verified: true, verification_reason: `policy=${topWebPolicy}; source_verified=true; matching_score=${nameSimilarity(nameWithBranch(mention.shop_name, mention.branch_name), candidate.name).toFixed(3)}; proof=${proof.method}; page_sha256=${proof.page_sha256}` };
    if (!pageVerifiedCandidateValid(mention, verified)) return [null, '候補ページの住所または支店が投稿の地域情報と一致しない'];
    this.journal.put(kind, key, JSON.stringify({ final_url: document.final_url, method: proof.method, page_sha256: proof.page_sha256, candidate: verified }));
    return [verified, null];
  }
}
export function candidateVerificationUrls(mention: ExtractedMention, candidates: readonly PipelineCandidate[]): readonly string[] {
  const urls: string[] = [];
  for (const candidate of candidates) {
    if (!matchesMentionNameAndArea(mention, candidate)) continue;
    const url = candidate.canonical_url || candidate.evidence_url;
    if (url && !urls.includes(url)) urls.push(url);
    if (urls.length === 2) break;
  }
  return urls;
}
