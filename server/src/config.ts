import { isIP } from 'node:net';
import { resolve } from 'node:path';
import { z } from 'zod';

export const discordId = z.string().refine(value => /^[1-9]\d{16,19}$/.test(value) && BigInt(value) <= 18446744073709551615n, 'DiscordのユーザーIDを正確な17〜20桁の数字で指定してください。');
const environmentSchema = z.object({
  APP_ENV: z.enum(['development', 'production']).default('development'),
  DATABASE_URL: z.string().default('sqlite:///meshi.db'),
  SECRET_KEY: z.string().min(32),
  HTTPS_ONLY: z.enum(['true', 'false']).default('false'),
  APP_READ_ONLY: z.enum(['true', 'false']).default('false'),
  ALLOW_ANONYMOUS_READ: z.literal('false').default('false'),
  ADMIN_USER_ID: z.string().default(''),
  WEB_ADMIN_USER_IDS: z.string().default(''),
  DISCORD_ALLOWED_USER_IDS: z.string().default(''),
  DISCORD_CLIENT_ID: z.string().min(1),
  DISCORD_CLIENT_SECRET: z.string().min(1),
  DISCORD_REDIRECT_URI: z.string().url(),
  DISCORD_GUILD_ID: z.string().default(''),
  DISCORD_CHANNEL_ID: z.string().default(''),
  DISCORD_TOKEN: z.string().default(''),
  FORWARDED_ALLOW_IPS: z.string().default(''),
  SHOP_IMAGE_CACHE_DIR: z.string().default('.shop-image-cache'),
  PORT: z.coerce.number().int().min(1).max(65535).default(8021),
  HOST: z.string().default('127.0.0.1'),
});
export interface Config {
  environment: 'development' | 'production';
  databaseUrl: string;
  secret: string;
  secureCookies: boolean;
  readOnly: boolean;
  admins: ReadonlySet<string>;
  configuredViewers: ReadonlySet<string>;
  clientId: string;
  clientSecret: string;
  redirectUri: string;
  guildId: string;
  channelId: string;
  botToken: string;
  trustedProxies: readonly string[];
  imageDirectory: string;
  port: number;
  host: string;
}
function userIds(value: string): Set<string> { return new Set(value.split(',').map(id => id.trim()).filter(Boolean).map(id => discordId.parse(id))); }
export function readConfig(environment: NodeJS.ProcessEnv = process.env): Config {
  const parsed = environmentSchema.safeParse(environment);
  if (!parsed.success) throw new Error(`Invalid server configuration: ${parsed.error.issues.map(issue => issue.path.join('.') + ': ' + issue.message).join('; ')}`);
  const env = parsed.data;
  const admins = userIds(env.WEB_ADMIN_USER_IDS);
  if (env.ADMIN_USER_ID) admins.add(discordId.parse(env.ADMIN_USER_ID));
  if (!admins.size) throw new Error('At least one Discord administrator must be configured');
  const redirect = new URL(env.DISCORD_REDIRECT_URI);
  if (!['https:', ...(env.APP_ENV === 'development' ? ['http:'] : [])].includes(redirect.protocol) || redirect.username || redirect.password || redirect.search || redirect.hash || redirect.pathname !== '/auth/discord/callback') throw new Error('DISCORD_REDIRECT_URI must be the absolute Discord callback URL');
  const trustedProxies = env.FORWARDED_ALLOW_IPS.split(',').map(value => value.trim()).filter(Boolean);
  for (const entry of trustedProxies) {
    const [address, mask, extra] = entry.split('/');
    const family = address ? isIP(address) : 0;
    if (!family || extra !== undefined || (mask !== undefined && (!/^\d+$/.test(mask) || Number(mask) < 1 || Number(mask) > (family === 4 ? 32 : 128)))) throw new Error('FORWARDED_ALLOW_IPS must contain explicit trusted addresses or networks');
  }
  if (env.APP_ENV === 'production' && (env.HTTPS_ONLY !== 'true' || !trustedProxies.length)) throw new Error('Production requires HTTPS_ONLY=true and explicit FORWARDED_ALLOW_IPS');
  return { environment: env.APP_ENV, databaseUrl: env.DATABASE_URL, secret: env.SECRET_KEY, secureCookies: env.HTTPS_ONLY === 'true', readOnly: env.APP_READ_ONLY === 'true', admins, configuredViewers: userIds(env.DISCORD_ALLOWED_USER_IDS), clientId: env.DISCORD_CLIENT_ID, clientSecret: env.DISCORD_CLIENT_SECRET, redirectUri: env.DISCORD_REDIRECT_URI, guildId: env.DISCORD_GUILD_ID, channelId: env.DISCORD_CHANNEL_ID, botToken: env.DISCORD_TOKEN, trustedProxies, imageDirectory: resolve(env.SHOP_IMAGE_CACHE_DIR), port: env.PORT, host: env.HOST };
}
