import { lookup } from 'node:dns/promises';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import type { LookupFunction } from 'node:net';
import { publicAddress } from './image-cache.js';
import { safeHttpUrl } from './links.js';

export interface HtmlDocument { html: string; final_url: string; }
export class HtmlFetchError extends Error { constructor(message: string, options?: ErrorOptions) { super(message, options); this.name = 'HtmlFetchError'; } }
interface ResourceKind { label: string; accept: string; types: readonly string[]; maxBytes: number; }
interface PublicResource { bytes: Buffer; type: string; final_url: string; }
const htmlKind: ResourceKind = { label: 'HTML', accept: 'text/html,application/xhtml+xml', types: ['text/html', 'application/xhtml+xml'], maxBytes: 1000000 };
const imageKind: ResourceKind = { label: 'Image', accept: 'image/jpeg,image/png,image/webp', types: ['image/jpeg', 'image/png', 'image/webp'], maxBytes: 20 * 1024 * 1024 };
export async function fetchHtmlDocument(value: string, signal?: AbortSignal): Promise<HtmlDocument> {
  const resource = await fetchPublic(value, htmlKind, signal);
  const charset = /charset\s*=\s*["']?([^;\s"']+)/i.exec(resource.type)?.[1] ?? 'utf-8';
  return { html: new TextDecoder(charset).decode(resource.bytes), final_url: resource.final_url };
}
// Same public-network checks as HTML pages, for an image on any public host (for example a shop page's og:image).
export async function fetchPublicImage(value: string, signal?: AbortSignal): Promise<{ bytes: Buffer; final_url: string }> {
  const resource = await fetchPublic(value, imageKind, signal);
  return { bytes: resource.bytes, final_url: resource.final_url };
}
async function fetchPublic(value: string, kind: ResourceKind, signal?: AbortSignal): Promise<PublicResource> {
  try { return await fetchResource(value, kind, signal); }
  catch (error) {
    if (error instanceof HtmlFetchError) throw error;
    throw new HtmlFetchError(`${kind.label} request failed: host=${safeHttpUrl(value) ? new URL(value).hostname : '(invalid)'}; reason=${error instanceof Error ? error.message : String(error)}`, { cause: error });
  }
}
async function fetchResource(value: string, kind: ResourceKind, signal?: AbortSignal): Promise<PublicResource> {
  let current = value;
  for (let redirects = 0; redirects < 6; redirects++) {
    const safe = safeHttpUrl(current); if (!safe) throw new HtmlFetchError(`${kind.label} URL must use HTTP or HTTPS without credentials`);
    const url = new URL(safe); let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_resolve, reject) => { timer = setTimeout(() => reject(new HtmlFetchError(`${kind.label} DNS lookup timed out: host=${url.hostname}`)), 5000); });
    const resolved = await (async () => { try { return await Promise.race([lookup(url.hostname.replace(/^\[|\]$/g, ''), { all: true, verbatim: true }), timeout]); } finally { clearTimeout(timer); } })();
    if (!resolved.length || resolved.some(item => !publicAddress(item.address))) throw new HtmlFetchError(`${kind.label} URL resolved outside public networks: host=${url.hostname}`);
    const selected = [...resolved].sort((a, b) => a.family - b.family || (a.address < b.address ? -1 : a.address > b.address ? 1 : 0))[0];
    if (!selected) throw new HtmlFetchError(`${kind.label} URL has no resolved address: host=${url.hostname}`);
    const pinned: LookupFunction = (_host, options, callback) => { if (options.all) callback(null, [selected]); else callback(null, selected.address, selected.family); };
    const activeSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000);
    const result = await new Promise<{ bytes: Buffer; type: string; location: string | null }>((resolve, reject) => {
      const request = url.protocol === 'https:' ? httpsRequest : httpRequest;
      const outgoing = request(url, { lookup: pinned, agent: false, signal: activeSignal, headers: { 'User-Agent': 'MeshiArchive/2.0 (+restaurant metadata fetch)', Accept: kind.accept } }, response => {
        const status = response.statusCode ?? 0;
        if ([301, 302, 303, 307, 308].includes(status)) {
          response.resume(); if (!response.headers.location) { reject(new HtmlFetchError(`${kind.label} redirect has no Location: host=${url.hostname} status=${status}`)); return; }
          resolve({ bytes: Buffer.alloc(0), type: '', location: response.headers.location }); return;
        }
        const type = response.headers['content-type'] ?? '';
        if (status >= 400 || !kind.types.some(allowed => type.toLowerCase().includes(allowed))) { response.resume(); reject(new HtmlFetchError(`${kind.label} response rejected: host=${url.hostname} status=${status} type=${type}`)); return; }
        const collect = async (): Promise<void> => {
          const chunks: Buffer[] = []; let size = 0;
          for await (const chunk of response) {
            if (!Buffer.isBuffer(chunk)) throw new HtmlFetchError(`${kind.label} stream was not binary: host=${url.hostname}`);
            size += chunk.length; if (size > kind.maxBytes) throw new HtmlFetchError(`${kind.label} content exceeds ${kind.maxBytes} bytes: host=${url.hostname} bytes=${size}`); chunks.push(chunk);
          }
          resolve({ bytes: Buffer.concat(chunks, size), type, location: null });
        };
        void collect().catch(reject);
      });
      outgoing.on('error', reject); outgoing.end();
    });
    if (result.location !== null) { current = new URL(result.location, current).href; continue; }
    return { bytes: result.bytes, type: result.type, final_url: current };
  }
  throw new HtmlFetchError(`${kind.label} redirect limit exceeded: host=${new URL(value).hostname}`);
}
