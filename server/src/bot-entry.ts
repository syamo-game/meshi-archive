import 'dotenv/config';
import { once } from 'node:events';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { Events } from 'discord.js';
import { z } from 'zod';
import { readAISettings } from './ai.js';
import { DiscordBot, readBotSettings } from './bot.js';
import { openDatabase } from './db/database.js';
import { verifyDatabase } from './db/migrate.js';
import { RestaurantProcessor } from './pipeline.js';

const settings = readBotSettings();
const { values } = parseArgs({ options: { sync: { type: 'boolean', default: false } } });
const database = openDatabase(process.env.DATABASE_URL || 'sqlite:///meshi.db');
const days = z.coerce.number().int().min(1).parse(process.env.LOOKUP_CACHE_DAYS ?? 30);
const processor = new RestaurantProcessor(database, readAISettings(), resolve(process.env.SHOP_IMAGE_CACHE_DIR || '.shop-image-cache'), days);
const bot = new DiscordBot(values.sync ? { ...settings, weekly: false } : settings, database, {
  processMessage: envelope => processor.processMessage(envelope, { allowSourceDiscovery: true }),
  cacheImage: url => processor.cacheImage(url),
  preflightModels: () => processor.preflightModels(),
});
let closing = false;
async function shutdown(): Promise<void> {
  if (closing) return; closing = true;
  try { await bot.stop(); await database.close(); }
  catch (error) { console.error('Bot shutdown failed', error); process.exitCode = 1; }
}
try {
  await verifyDatabase(database); await bot.start();
  if (values.sync) {
    if (!bot.client.isReady()) await once(bot.client, Events.ClientReady);
    try { await bot.runWeekly(true); } finally { await shutdown(); }
  }
}
catch (error) { console.error('Bot startup refused', error); await shutdown(); throw error; }
process.once('SIGTERM', () => { void shutdown(); });
process.once('SIGINT', () => { void shutdown(); });
