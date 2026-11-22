import type { RestaurantAI } from './ai.js';
import type { AreaChoice } from './ai-models.js';
import { refineAreaByAddress } from './candidate-geography.js';
import type { Executor } from './db/database.js';
import { areas, wardOnlyArea } from './geography.js';
import type { PipelineJournal } from './pipeline-journal.js';
import { areaMunicipality, regionMaster } from './region-master.js';
import { shopRecord } from './records.js';
import type { ShopRecord } from './records.js';

// Master areas inside the same ward, offered to the model as the only answers.
export function finerAreaChoices(area: string): readonly AreaChoice[] {
  const municipality = areaMunicipality(area); if (!municipality) return [];
  return Object.keys(regionMaster.leaf_areas[municipality.code] ?? {}).flatMap(value => { const entry = areas.get(value); return entry ? [{ value, label: entry.display }] : []; });
}

// A Tokyo ward alone is too coarse. The address names the town, so set the station or town area it belongs to.
// The address table is tried first; the model is asked only when the town name differs from every area name.
export async function refineShopArea(connection: Executor, ai: RestaurantAI, journal: PipelineJournal, messageId: string, shop: ShopRecord): Promise<readonly [ShopRecord, string | null]> {
  if (!shop.area || !shop.address || !wardOnlyArea(shop.area)) return [shop, null];
  let area = refineAreaByAddress(shop.area, shop.address) ?? shop.area; let reason = '住所の町名から判定';
  if (area === shop.area) {
    const choices = finerAreaChoices(shop.area); if (!choices.length) return [shop, null];
    const result = await ai.nearestArea(shop.address, choices).catch((error: unknown) => { if (error instanceof Error) journal.failure(messageId, 'area_refinement', error); throw error; });
    journal.metrics(messageId, 'area_refinement', result.metrics);
    if (result.area === null) return [shop, `住所から細かいエリアを判定できない: ${result.reason}`];
    area = result.area; reason = result.reason;
  }
  const updated = (await connection.query('UPDATE shops SET area=$1,updated_at=$2,version=version+1 WHERE id=$3 AND version=$4 RETURNING *', shopRecord, [area, new Date().toISOString(), shop.id, shop.version]))[0];
  if (!updated) throw new Error(`Shop changed while refining its area: shop_id=${shop.id} version=${shop.version} area=${area}`);
  return [updated, `エリアを${shop.area}から${areas.get(area)?.display ?? area}に変更（${reason}）`];
}
