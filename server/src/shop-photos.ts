import { z } from 'zod';
import type { Database } from './db/database.js';
import { fetchHtmlDocument, fetchPublicImage } from './html-fetch.js';
import { downloadImage } from './image-cache.js';
import { imageHosts, storeUploadedImage } from './images.js';
import type { MessageEnvelope } from './ingestion.js';
import { safeHttpUrl } from './links.js';

export type PhotoSource = 'post_image' | 'shop_page_og_image';
export interface PhotoFetchers { postImage(url: string): Promise<Uint8Array>; page(url: string): Promise<{ html: string; final_url: string }>; pageImage(url: string): Promise<Uint8Array>; }
export interface AttachedPhoto { shop_id: number; source: PhotoSource; url: string; }
const defaultFetchers: PhotoFetchers = {
  postImage: url => downloadImage(url),
  page: url => fetchHtmlDocument(url),
  pageImage: async url => (await fetchPublicImage(url)).bytes,
};
const shopRow = z.object({ id: z.number().int(), canonical_url: z.string().nullable() });

// Read the page's share image (og:image, then twitter:image) as an absolute HTTP URL.
export function pageShareImage(html: string, pageUrl: string): string | null {
  for (const key of ['og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image']) {
    for (const tag of html.match(/<meta\b[^>]*>/gi) ?? []) {
      const name = /\b(?:property|name)\s*=\s*["']([^"']+)["']/i.exec(tag)?.[1]?.trim().toLowerCase();
      const content = /\bcontent\s*=\s*["']([^"']+)["']/i.exec(tag)?.[1]?.trim();
      if (name !== key || !content) continue;
      try { const absolute = safeHttpUrl(new URL(content.replaceAll('&amp;', '&'), pageUrl).href); if (absolute) return absolute; }
      catch (error) { if (!(error instanceof TypeError)) throw error; }
    }
  }
  return null;
}
function postImageUrl(envelope: MessageEnvelope): string | null {
  for (const asset of envelope.assets) {
    if (asset.kind !== 'image') continue;
    const host = new URL(asset.url).hostname.toLowerCase().replace(/\.$/, '');
    if (imageHosts.has(host)) return asset.url;
  }
  return null;
}

// Give each shop this post resolved a photo when it has none: the post's own image when the post names one shop,
// otherwise the share image on the shop's page. A shop with no usable image stays without one and the reason is logged.
export async function attachMissingShopPhotos(database: Database, directory: string, envelope: MessageEnvelope, fetchers: PhotoFetchers = defaultFetchers): Promise<AttachedPhoto[]> {
  const resolved = await database.query("SELECT DISTINCT shop_id FROM shop_mentions WHERE message_id=$1 AND review_status='approved' AND shop_id IS NOT NULL", z.object({ shop_id: z.number().int() }), [envelope.message_id]);
  if (!resolved.length) return [];
  const ids = resolved.map(row => row.shop_id);
  const shops = await database.query(`SELECT id,canonical_url FROM shops WHERE image_key IS NULL AND id IN (${ids.map((_, index) => '$' + (index + 1)).join(',')}) ORDER BY id`, shopRow, ids);
  const postImage = resolved.length === 1 ? postImageUrl(envelope) : null; const attached: AttachedPhoto[] = [];
  for (const shop of shops) {
    const tried: string[] = [];
    const sources: (() => Promise<{ source: PhotoSource; url: string; bytes: Uint8Array } | null>)[] = [
      async () => postImage ? { source: 'post_image', url: postImage, bytes: await fetchers.postImage(postImage) } : null,
      async () => {
        if (!shop.canonical_url) return null;
        const page = await fetchers.page(shop.canonical_url); const image = pageShareImage(page.html, page.final_url);
        if (!image) { tried.push(`shop_page_og_image: no share image on ${page.final_url}`); return null; }
        return { source: 'shop_page_og_image', url: image, bytes: await fetchers.pageImage(image) };
      },
    ];
    for (const next of sources) {
      try {
        const found = await next(); if (!found) continue;
        const stored = await storeUploadedImage(found.bytes, directory);
        // Do not replace a photo someone set while this ran.
        const changed = await database.execute('UPDATE shops SET image_key=$1,updated_at=$2,version=version+1 WHERE id=$3 AND image_key IS NULL', [stored.key, new Date().toISOString(), shop.id]);
        if (changed) attached.push({ shop_id: shop.id, source: found.source, url: found.url });
        break;
      } catch (error) {
        tried.push(error instanceof Error ? error.message : String(error));
        console.error('Shop photo source failed', { messageId: envelope.message_id, shopId: shop.id, canonicalUrl: shop.canonical_url, error });
      }
    }
    if (!attached.some(item => item.shop_id === shop.id)) console.warn('Shop photo not found', { messageId: envelope.message_id, shopId: shop.id, canonicalUrl: shop.canonical_url, postImage, tried });
  }
  return attached;
}
