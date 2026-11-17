import { z } from 'zod';

// X blocks normal page fetches (403), so read posts from the public embed endpoint instead.
const defaultEndpoint = 'https://cdn.syndication.twimg.com/tweet-result';
const statusPattern = /^https?:\/\/(?:www\.|mobile\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]{1,15}\/status(?:es)?\/(\d{1,20})(?:[/?#]|$)/i;

const postSchema: z.ZodType<RawPost> = z.lazy(() => z.object({
  id_str: z.string().regex(/^\d{1,20}$/),
  text: z.string(),
  user: z.object({ name: z.string(), screen_name: z.string() }).optional(),
  photos: z.array(z.object({ url: z.string().url() })).optional(),
  quoted_tweet: postSchema.optional(),
}));
interface RawPost { id_str: string; text: string; user?: { name: string; screen_name: string } | undefined; photos?: { url: string }[] | undefined; quoted_tweet?: RawPost | undefined; }

export interface XPost { id: string; author: string | null; text: string; photo_urls: readonly string[]; quoted: XPost | null; }
export class XPostError extends Error { constructor(message: string, options?: ErrorOptions) { super(message, options); this.name = 'XPostError'; } }

export function xStatusId(url: string): string | null { return statusPattern.exec(url.trim())?.[1] ?? null; }

// The endpoint needs a token derived from the post ID, the same way X's own embed script builds it.
function embedToken(id: string): string { return ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, ''); }

function toPost(raw: RawPost): XPost {
  return { id: raw.id_str, author: raw.user ? `${raw.user.name} (@${raw.user.screen_name})` : null, text: raw.text, photo_urls: (raw.photos ?? []).map(photo => photo.url), quoted: raw.quoted_tweet ? toPost(raw.quoted_tweet) : null };
}

export async function fetchXPost(id: string, endpoint: string = process.env.X_EMBED_ENDPOINT || defaultEndpoint, signal: AbortSignal = AbortSignal.timeout(15000)): Promise<XPost> {
  if (!/^\d{1,20}$/.test(id)) throw new XPostError(`Invalid X post ID: id=${id}`);
  const url = new URL(endpoint); url.searchParams.set('id', id); url.searchParams.set('lang', 'ja'); url.searchParams.set('token', embedToken(id));
  let response: Response;
  try { response = await fetch(url, { signal, headers: { accept: 'application/json' } }); }
  catch (error) { throw new XPostError(`X post request failed: id=${id} host=${url.hostname}`, { cause: error }); }
  if (!response.ok) throw new XPostError(`X post request failed: id=${id} status=${response.status} body=${(await response.text()).slice(0, 200)}`);
  const parsed = postSchema.safeParse(await response.json());
  if (!parsed.success) throw new XPostError(`X post response did not match its contract: id=${id} issues=${parsed.error.issues.map(issue => `${issue.path.join('.') || '(root)'}: ${issue.message}`).join('; ')}`);
  if (parsed.data.id_str !== id) throw new XPostError(`X post response returned another post: id=${id} returned=${parsed.data.id_str}`);
  return toPost(parsed.data);
}

// Text block added to the message so the model reads the full post, not the cut-off Discord preview.
export function xPostText(post: XPost): string {
  const lines = [`[X Post Full Text] id=${post.id}${post.author ? ` author=${post.author}` : ''}`, post.text, `[X Post Photos] ${post.photo_urls.length}`];
  if (post.quoted) lines.push(`[X Quoted Post] id=${post.quoted.id}${post.quoted.author ? ` author=${post.quoted.author}` : ''}`, post.quoted.text);
  return lines.join('\n');
}
