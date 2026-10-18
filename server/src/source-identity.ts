import { createHash } from 'node:crypto';
import { casefold, compareText, quotePlus } from './text.js';

export interface SourceIdentity { service: 'x' | 'youtube' | 'instagram' | 'facebook' | 'tiktok' | 'threads'; item_id: string; normalized_url: string; }
export interface AssetIdentity { source_service: SourceIdentity['service'] | null; source_item_id: string | null; normalized_url: string; }
const tracking = new Set(['fbclid', 'feature', 'si', 's', 't', 'utm_campaign', 'utm_content', 'utm_medium', 'utm_source', 'utm_term']);
export function normalizeAssetUrl(value: string): string | null {
  const cleaned = value.normalize('NFKC').trim();
  const match = /^(https?):\/\/([^/?#]+)([^?#]*)(?:\?([^#]*))?(?:#.*)?$/is.exec(cleaned);
  if (!match?.[1] || !match[2]) return null;
  let parsed: URL;
  try { parsed = new URL(cleaned); } catch { return null; }
  if (parsed.username || parsed.password || !parsed.hostname) return null;
  const hostMatch = /^(\[[^\]]+\]|[^:]+)(?::(\d+))?$/.exec(match[2]);
  if (!hostMatch?.[1] || hostMatch[1].includes('@')) return null;
  const protocol = match[1].toLowerCase(); const port = hostMatch[2] ? Number(hostMatch[2]) : null;
  const host = hostMatch[1].toLowerCase();
  const authority = host + (port !== null && !(protocol === 'https' && port === 443) && !(protocol === 'http' && port === 80) ? ':' + port : '');
  let path = (match[3] || '/').replace(/;[^/]*$/, '').replace(/\/{2,}/g, '/');
  if (path !== '/') path = path.replace(/\/+$/, '');
  const pairs = [...new URLSearchParams(match[4] ?? '')].filter(([key]) => !tracking.has(casefold(key))).sort(([a, b], [c, d]) => compareText(a, c) || compareText(b, d));
  const query = pairs.map(([key, item]) => quotePlus(key) + '=' + quotePlus(item)).join('&');
  return `${protocol}://${authority}${path}${query ? '?' + query : ''}`;
}
export function identifySourceUrl(value: string): SourceIdentity | null {
  const normalized = normalizeAssetUrl(value);
  if (!normalized) return null;
  const url = new URL(normalized); const host = url.hostname; const path = url.pathname;
  if (['x.com', 'www.x.com', 'mobile.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'].includes(host)) {
    const id = /\/(?:[^/]+|i\/web)\/status\/(\d+)(?:\/|$)/i.exec(path)?.[1];
    if (id) return { service: 'x', item_id: id, normalized_url: `https://x.com/i/status/${id}` };
  }
  if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'].includes(host)) {
    const id = host === 'youtu.be' ? path.replace(/^\/+/, '').split('/')[0] : url.searchParams.get('v') || /\/(?:shorts|live|embed)\/(?:[^/]*?\/)?([A-Za-z0-9_-]{6,64})(?:\/|$)/.exec(path)?.[1];
    if (id && /^[A-Za-z0-9_-]{3,255}$/.test(id)) return { service: 'youtube', item_id: id, normalized_url: `https://www.youtube.com/watch?v=${id}` };
  }
  if (['instagram.com', 'www.instagram.com', 'm.instagram.com'].includes(host)) {
    const id = /\/(?:p|reel|reels|tv)\/([A-Za-z0-9_-]+)(?:\/|$)/i.exec(path)?.[1];
    if (id) return { service: 'instagram', item_id: id, normalized_url: `https://www.instagram.com/p/${id}/` };
  }
  if (['facebook.com', 'www.facebook.com', 'm.facebook.com', 'fb.watch'].includes(host)) {
    const id = url.searchParams.get('v') || /\/(?:[^/]+\/)?posts\/(\d+)(?:\/|$)/i.exec(path)?.[1] || /\/(?:reel|videos)\/(\d+)(?:\/|$)/i.exec(path)?.[1] || /\/share\/(?:p|r)\/([A-Za-z0-9_-]+)(?:\/|$)/i.exec(path)?.[1];
    if (id && id.length <= 255) return { service: 'facebook', item_id: id, normalized_url: `https://www.facebook.com/watch/?v=${id}` };
  }
  if (['tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'].includes(host)) {
    const id = /\/(?:@[^/]+\/)?video\/(\d+)(?:\/|$)/i.exec(path)?.[1];
    if (id) return { service: 'tiktok', item_id: id, normalized_url: `https://www.tiktok.com/video/${id}` };
  }
  if (['threads.net', 'www.threads.net'].includes(host)) {
    const id = /\/(?:@[^/]+\/)?post\/([A-Za-z0-9_-]+)(?:\/|$)/i.exec(path)?.[1];
    if (id) return { service: 'threads', item_id: id, normalized_url: `https://www.threads.net/post/${id}` };
  }
  return null;
}
export function sourceAssetIdentity(value: string): AssetIdentity | null {
  const source = identifySourceUrl(value);
  if (source) return { source_service: source.service, source_item_id: source.item_id, normalized_url: source.normalized_url };
  const normalized = normalizeAssetUrl(value);
  return normalized ? { source_service: null, source_item_id: null, normalized_url: normalized } : null;
}
export function sourceAssetFingerprint(input: { kind: string; normalized_url: string; title: string | null; description: string | null; extracted_text?: string | null }): string {
  const normalize = (value: string | null | undefined): string => casefold((value ?? '').normalize('NFKC')).replace(/\s+/gu, ' ').trim();
  return createHash('sha256').update([casefold(input.kind.trim()), input.normalized_url, normalize(input.title), normalize(input.description), normalize(input.extracted_text)].join('\x1f')).digest('hex');
}
