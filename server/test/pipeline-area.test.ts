import assert from 'node:assert/strict';
import { test } from 'node:test';
import { z } from 'zod';
import { readAISettings, RestaurantAI } from '../src/ai.js';
import type { Database } from '../src/db/database.js';
import { refineShopArea } from '../src/pipeline-area.js';
import { PipelineJournal } from '../src/pipeline-journal.js';
import { shopRecord } from '../src/records.js';
import type { ShopRecord } from '../src/records.js';
import { anthropicMessage, fixture, withJsonServer } from './helpers.js';

const requestSchema = z.object({ messages: z.array(z.object({ content: z.string() })), output_config: z.object({ format: z.object({ schema: z.object({ properties: z.object({ area: z.object({ enum: z.array(z.string()) }) }) }) }) }) });

async function insertShop(database: Database, area: string, address: string | null): Promise<ShopRecord> {
  const now = new Date().toISOString();
  const shop = (await database.query('INSERT INTO shops(shop_name,area,category,address,is_visited,created_at,updated_at,version) VALUES($1,$2,$3,$4,$5,$6,$7,1) RETURNING *', shopRecord, ['合成食堂', area, '居酒屋', address, false, now, now]))[0];
  if (!shop) throw new Error('Test shop insertion returned no row'); return shop;
}

async function withModel(answer: { area: string; reason: string }, run: (ai: RestaurantAI, requests: () => readonly string[]) => Promise<void>): Promise<void> {
  const previous = process.env.ANTHROPIC_BASE_URL;
  try {
    await withJsonServer(() => ({ status: 200, body: anthropicMessage([{ type: 'text', text: JSON.stringify(answer) }]) }), async (baseUrl, requests) => {
      process.env.ANTHROPIC_BASE_URL = baseUrl;
      await run(new RestaurantAI(readAISettings({ ANTHROPIC_API_KEY: 'test-key' }), '.local/no-ai-fixture'), () => requests.map(request => request.body));
    });
  } finally { if (previous === undefined) delete process.env.ANTHROPIC_BASE_URL; else process.env.ANTHROPIC_BASE_URL = previous; }
}

test('a ward-only shop whose town differs from every station name gets the area the model picks from that ward', async () => {
  const { database } = await fixture();
  try {
    await withModel({ area: '南砂町', reason: '南砂4丁目は南砂町駅の近く' }, async (ai, requests) => {
      const shop = await insertShop(database, '東京都江東区', '東京都江東区南砂4丁目1-1');
      const [updated, note] = await refineShopArea(database, ai, new PipelineJournal(readAISettings({ ANTHROPIC_API_KEY: 'test-key' })), '90000000000000301', shop);
      assert.equal(updated.area, '南砂町'); assert.equal(updated.version, shop.version + 1);
      assert.match(note ?? '', /江東区から南砂町に変更（南砂4丁目は南砂町駅の近く）/);
      const [body] = requests(); const sent = requestSchema.parse(JSON.parse(body ?? '{}'));
      const choices = sent.output_config.format.schema.properties.area.enum;
      assert.ok(choices.includes('南砂町') && choices.includes('unknown')); assert.ok(!choices.includes('月島'), 'only areas of the same ward are offered');
      assert.match(sent.messages[0]?.content ?? '', /住所: 東京都江東区南砂4丁目1-1/);
    });
  } finally { await database.close(); }
});

test('a ward-only shop whose address names a master area is set without asking the model', async () => {
  const { database } = await fixture();
  try {
    await withModel({ area: 'unknown', reason: '未使用' }, async (ai, requests) => {
      const shop = await insertShop(database, '東京都中央区', '東京都中央区晴海5丁目6番7');
      const [updated] = await refineShopArea(database, ai, new PipelineJournal(readAISettings({ ANTHROPIC_API_KEY: 'test-key' })), '90000000000000302', shop);
      assert.equal(updated.area, '東京都中央区 / 晴海'); assert.equal(requests().length, 0);
    });
  } finally { await database.close(); }
});

test('the ward stays for review when the model cannot tell, and shops without an address are left alone', async () => {
  const { database } = await fixture();
  try {
    await withModel({ area: 'unknown', reason: '番地だけでは判断できない' }, async (ai, requests) => {
      const journal = new PipelineJournal(readAISettings({ ANTHROPIC_API_KEY: 'test-key' }));
      const shop = await insertShop(database, '東京都江東区', '東京都江東区南砂4丁目1-1');
      const [kept, note] = await refineShopArea(database, ai, journal, '90000000000000303', shop);
      assert.equal(kept.area, '東京都江東区'); assert.equal(kept.version, shop.version); assert.match(note ?? '', /判定できない: 番地だけでは判断できない/);
      const bare = await insertShop(database, '東京都江東区', null);
      const [untouched, none] = await refineShopArea(database, ai, journal, '90000000000000304', bare);
      assert.equal(untouched.area, '東京都江東区'); assert.equal(none, null); assert.equal(requests().length, 1);
    });
  } finally { await database.close(); }
});
