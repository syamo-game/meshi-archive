import type { RestaurantAI } from './ai.js';
import type { ClueCandidate } from './ai-models.js';
import { imageHosts } from './images.js';
import type { MessageEnvelope } from './ingestion.js';
import { pipelineCandidateSchema } from './pipeline-candidates.js';
import type { PipelineCandidate } from './pipeline-candidates.js';
import type { PipelineJournal } from './pipeline-journal.js';

export interface ClueOutcome { candidates: PipelineCandidate[]; note: string; }
const confidenceLabel: Readonly<Record<ClueCandidate['confidence'], string>> = { high: '高', medium: '中', low: '低' };

// Photos the AI may look at: cached images from allowed hosts, in post order.
export function cluePhotos(envelope: MessageEnvelope): string[] {
  return [...new Set(envelope.assets.filter(asset => asset.kind === 'image' && imageHosts.has(new URL(asset.url).hostname.toLowerCase().replace(/\.$/, ''))).map(asset => asset.url))];
}
function urlKey(value: string): string { const url = new URL(value); url.hash = ''; return url.href.replace(/\/$/, ''); }

// Search for the shop of a restaurant post that names none. Candidates are never approved here; they go to review.
export async function searchClues(ai: RestaurantAI, journal: PipelineJournal, envelope: MessageEnvelope): Promise<ClueOutcome> {
  const photos = cluePhotos(envelope); const found: ClueCandidate[] = []; const sources = new Set<string>(); const reasons: string[] = [];
  const run = async (batch: readonly string[]): Promise<boolean> => {
    const result = await ai.clueSearch(envelope.content, batch).catch(error => { if (error instanceof Error) journal.failure(envelope.message_id, 'clue_search', error); throw error; });
    journal.metrics(envelope.message_id, 'clue_search', result.metrics);
    for (const url of result.source_urls) sources.add(urlKey(url));
    found.push(...result.search.candidates); if (result.search.unresolved_reason) reasons.push(result.search.unresolved_reason);
    return result.search.is_list_post;
  };
  // A list post (rankings and the like) often has one shop per photo, so read the next four photos too.
  if (await run(photos.slice(0, 4)) && photos.length > 4) await run(photos.slice(4, 8));
  const candidates: PipelineCandidate[] = []; const seen = new Set<string>(); let unverified = 0;
  for (const candidate of found) {
    const name = [candidate.shop_name, candidate.branch_name].filter(Boolean).join(' ');
    if (seen.has(name)) continue; seen.add(name);
    // Keep only evidence the searches actually looked at, so the model cannot cite a page it never saw.
    const evidence = candidate.evidence_urls.filter(url => { try { return sources.has(urlKey(url)); } catch (error) { if (error instanceof TypeError) return false; throw error; } });
    if (!evidence.length) { unverified++; continue; }
    const conflict = candidate.area_matches_post === 'conflict' ? ' / 投稿の地名と所在地が食い違う' : '';
    candidates.push(pipelineCandidateSchema.parse({
      name, area: candidate.area, category: null, address: null, phone: null, canonical_url: null, external_source: null, external_id: null, evidence_url: evidence[0] ?? null,
      provenance: 'web_search', is_verified: false,
      verification_reason: [...`手がかり検索 確信度=${confidenceLabel[candidate.confidence]}${conflict} / 一致: ${candidate.matched_clues.join('; ')}`].slice(0, 2000).join(''),
    }));
  }
  const parts = [`手がかり検索: 候補${candidates.length}件`];
  if (unverified) parts.push(`検索で根拠を確認できない候補${unverified}件を除外`);
  if (!candidates.length && reasons.length) parts.push(reasons.join(' / '));
  return { candidates, note: parts.join('。') };
}
