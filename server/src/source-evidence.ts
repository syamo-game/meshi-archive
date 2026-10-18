import type { SearchCandidate } from './ai-models.js';
import { normalizeExternalIdentity, normalizeExternalSource } from './resolution.js';
import { externalIdentity } from './shop-creation.js';
import { casefold, compareText, quotePlus } from './text.js';

const aliases: Readonly<Record<string, string>> = { 'fixupx.com': 'x.com', 'fixvx.com': 'x.com', 'fxtwitter.com': 'x.com', 'mobile.twitter.com': 'x.com', 'm.twitter.com': 'x.com', 'twitter.com': 'x.com', 'vxtwitter.com': 'x.com', 'mobile.x.com': 'x.com', 'm.x.com': 'x.com', 'music.youtube.com': 'youtube.com', 's.tabelog.com': 'tabelog.com', 'youtube-nocookie.com': 'youtube.com' };
function pathOf(url: URL): string { return url.pathname.replace(/;[^/]*$/, '').replace(/\/+$/, '') || '/'; }
export function sourceEvidenceUrlIdentity(value: string): string {
  const external = externalIdentity(value); if (external) return `external:${normalizeExternalSource(external[0])}:${casefold(external[1].trim())}`;
  const parsed = new URL(value); let host = parsed.hostname.toLowerCase().replace(/^www\./, ''); host = aliases[host] ?? host; const path = pathOf(parsed);
  if (host === 'x.com') { const id = /\/(?:[^/]+\/)*status\/([0-9]+)/.exec(path)?.[1]; if (id) return `x.com/status/${id}`; }
  if (host === 'youtu.be') { const id = path.replace(/^\/+/, '').split('/')[0]; if (id) return `youtube.com/video/${id}`; }
  if (host === 'youtube.com' || host === 'm.youtube.com') { const id = parsed.searchParams.get('v') || /^\/(?:shorts|embed|live)\/([^/]+)/.exec(path)?.[1]; if (id) return `youtube.com/video/${id}`; }
  return host + path;
}
export function citationUrlIdentity(value: string): string {
  const identity = sourceEvidenceUrlIdentity(value); if (identity.startsWith('external:') || identity.startsWith('x.com/status/') || identity.startsWith('youtube.com/video/')) return identity;
  const url = new URL(value); const host = url.hostname.toLowerCase().replace(/^www\./, ''); const path = pathOf(url);
  const query = [...url.searchParams].filter(([name]) => !casefold(name).startsWith('utm_') && !['fbclid', 'gclid', 'yclid'].includes(casefold(name))).sort(([a, b], [c, d]) => compareText(a, c) || compareText(b, d)).map(([name, item]) => quotePlus(name) + '=' + quotePlus(item)).join('&');
  return host + path + (query ? '?' + query : '');
}
export function hasCitedSource(candidate: Pick<SearchCandidate, 'canonical_url' | 'evidence_url'>, urls: readonly string[]): boolean {
  const sources = new Set(urls.map(citationUrlIdentity)); return [candidate.canonical_url, candidate.evidence_url].some(url => url !== null && sources.has(citationUrlIdentity(url)));
}
export function sanitizeCitedCandidate(candidate: SearchCandidate, urls: readonly string[]): SearchCandidate | null {
  const sources = new Set(urls.map(citationUrlIdentity));
  const canonical = candidate.canonical_url && sources.has(citationUrlIdentity(candidate.canonical_url)) ? candidate.canonical_url : null;
  const evidence = candidate.evidence_url && sources.has(citationUrlIdentity(candidate.evidence_url)) ? candidate.evidence_url : null;
  if (!canonical && !evidence) return null;
  const external = externalIdentity(canonical) ?? externalIdentity(evidence); const normalized = external ? normalizeExternalIdentity(...external) : null;
  return { ...candidate, canonical_url: canonical, evidence_url: evidence, external_source: normalized?.[0] ?? null, external_id: normalized?.[1] ?? null };
}
