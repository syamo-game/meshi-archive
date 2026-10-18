import { isIP } from 'node:net';

export function safeHttpUrl(value: string | null): string | null {
  if (!value) return null;
  const cleaned = value.trim();
  if (!cleaned || /[\s\u0000-\u001f\u007f\\]/u.test(cleaned)) return null;
  let url: URL;
  try { url = new URL(cleaned); } catch { return null; }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !url.hostname) return null;
  const host = url.hostname.replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!isIP(host) && (host.length > 253 || host.split('.').some(label => !/^[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?$/i.test(label)))) return null;
  return cleaned;
}
export function urlIdentity(value: string): string {
  const url = new URL(value);
  url.hash = ''; url.hostname = url.hostname.replace(/\.$/, '');
  if (url.pathname !== '/') url.pathname = url.pathname.replace(/\/+$/, '');
  const pairs = [...url.searchParams].filter(([name]) => !name.toLowerCase().startsWith('utm_') && !['fbclid', 'gclid', 'mc_cid', 'mc_eid'].includes(name.toLowerCase())).sort(([a, b], [c, d]) => a < c ? -1 : a > c ? 1 : b < d ? -1 : b > d ? 1 : 0);
  url.search = new URLSearchParams(pairs).toString();
  return url.href;
}
export function googleMapsUrl(value: string | null): boolean {
  if (!value) return false;
  const url = new URL(value); const host = url.hostname.toLowerCase().replace(/\.$/, '');
  return host === 'maps.app.goo.gl' || (host === 'goo.gl' && url.pathname.startsWith('/maps')) || (['google.com', 'www.google.com', 'maps.google.com', 'google.co.jp', 'www.google.co.jp', 'maps.google.co.jp'].includes(host) && url.pathname.startsWith('/maps'));
}
export function sourceLabel(value: string): string {
  const url = new URL(value); const host = url.hostname.toLowerCase();
  let path: string;
  try { path = decodeURIComponent(url.pathname); } catch { path = url.pathname; }
  if (path.toLowerCase().endsWith('.pdf')) return 'PDF';
  if (['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'].includes(host) || (['mobile.x.com', 'mobile.twitter.com'].includes(host) && /^\/(?:[^/]+|i\/web)\/status\/\d+(?:\/|$)/.test(path))) return 'X';
  if (host === 'tabelog.com' || host.endsWith('.tabelog.com')) return '食べログ';
  if (host === 'dancyu.jp' || host.endsWith('.dancyu.jp')) return 'dancyu';
  if (['youtube.com', 'youtu.be'].includes(host) || host.endsWith('.youtube.com')) return 'YouTube';
  if (host === 'discord.com' || host.endsWith('.discord.com')) return 'Discord';
  return host.replace(/^www\./, '');
}
export function tabelogIdentity(value: string | null): string | null {
  if (!value) return null;
  const url = new URL(value);
  if (url.hostname !== 'tabelog.com' && !url.hostname.endsWith('.tabelog.com')) return null;
  return /\/(\d{6,})(?:\/|$)/.exec(url.pathname)?.[1] ?? null;
}
