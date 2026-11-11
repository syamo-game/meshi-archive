import { ChannelType, Client, Events, GatewayIntentBits, Partials } from 'discord.js';
import type { Message, SendableChannels } from 'discord.js';
import { z } from 'zod';
import { discordId } from './config.js';
import type { Database } from './db/database.js';
import { acquireMessageEnvelope, discordInput } from './ingestion.js';
import type { MessageProcessor } from './ingestion.js';
import { discordHistory, HistorySyncError, nextWeeklyRun, SyncAlreadyRunning, syncChannelHistory, syncReport, weeklySlot } from './sync.js';
import type { SyncSummary } from './sync.js';

export interface BotSettings { token: string; adminId: string; channelId: string | null; weekly: boolean; batchLimit: number; readOnly: boolean; }
export function readBotSettings(environment: NodeJS.ProcessEnv = process.env): BotSettings {
  const parsed = z.object({ DISCORD_TOKEN: z.string().min(1), ADMIN_USER_ID: discordId, DISCORD_CHANNEL_ID: z.string().default(''), WEEKLY_SYNC_ENABLED: z.string().default('false').transform(value => value.trim().toLowerCase()).pipe(z.enum(['true', 'false'])), SYNC_BATCH_LIMIT: z.coerce.number().int().min(1).max(Number.MAX_SAFE_INTEGER).default(500), APP_READ_ONLY: z.enum(['true', 'false']).default('false') }).safeParse(environment);
  if (!parsed.success) throw new Error(`Invalid Bot configuration: ${parsed.error.issues.map(issue => issue.path.join('.') + ': ' + issue.message).join('; ')}`);
  const env = parsed.data;
  const channelId = env.DISCORD_CHANNEL_ID ? discordId.parse(env.DISCORD_CHANNEL_ID) : null;
  if (env.WEEKLY_SYNC_ENABLED === 'true' && !channelId) throw new Error('Weekly sync requires DISCORD_CHANNEL_ID');
  return { token: env.DISCORD_TOKEN, adminId: env.ADMIN_USER_ID, channelId, weekly: env.WEEKLY_SYNC_ENABLED === 'true', batchLimit: env.SYNC_BATCH_LIMIT, readOnly: env.APP_READ_ONLY === 'true' };
}
export class DiscordBot {
  readonly client: Client;
  private weeklyTimer: NodeJS.Timeout | null = null;
  private stopped = false;
  private readonly pending = new Set<Promise<void>>();
  constructor(private readonly settings: BotSettings, private readonly database: Database, private readonly processor: MessageProcessor) {
    this.client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.DirectMessages, GatewayIntentBits.MessageContent], partials: [Partials.Channel] });
    this.client.on(Events.Error, error => { console.error('Discord client failed', { error }); });
    this.client.on(Events.Warn, message => { console.warn('Discord client warning', { message }); });
    this.client.on(Events.ClientReady, client => {
      console.info('Bot ready', { userId: client.user.id });
      if (this.settings.weekly && !this.weeklyTimer) this.schedule();
    });
    this.client.on(Events.MessageCreate, message => {
      if (this.stopped) return;
      this.track(this.message(message).catch(error => { console.error('Discord message handler failed', { messageId: message.id, error }); }));
    });
  }
  async start(): Promise<void> {
    if (this.settings.readOnly) throw new Error('Bot startup refused because APP_READ_ONLY=true');
    await this.processor.preflightModels();
    try { await this.client.login(this.settings.token); }
    catch (error) { await this.client.destroy(); throw error; }
  }
  async stop(): Promise<void> {
    this.stopped = true;
    if (this.weeklyTimer) { clearTimeout(this.weeklyTimer); this.weeklyTimer = null; }
    await Promise.all(this.pending);
    await this.client.destroy();
  }
  private track(operation: Promise<void>): void {
    this.pending.add(operation);
    void operation.finally(() => { this.pending.delete(operation); });
  }
  private schedule(): void {
    if (this.stopped) return;
    const next = nextWeeklyRun(new Date());
    console.info('Weekly sync scheduled', { channelId: this.settings.channelId, nextRun: next.toISOString() });
    this.weeklyTimer = setTimeout(() => {
      this.weeklyTimer = null;
      this.track(this.runWeekly().then(result => result ? this.notifyAdmin(syncReport('週次同期', result)) : undefined, async error => {
        console.error('Weekly channel sync failed', { channelId: this.settings.channelId, error });
        await this.notifyAdmin(error instanceof HistorySyncError ? `週次同期を停止しました。${error.failed.length}件が続けて失敗しました。失敗した投稿は次回の同期で再試行します。message_id=${error.failed.join(', ')}` : `週次同期に失敗しました。channel_id=${this.settings.channelId}。Botのログを確認してください。`);
      }).finally(() => { this.schedule(); }));
    }, Math.max(1, next.getTime() - Date.now()));
    this.weeklyTimer.unref();
  }
  // Weekly sync has no channel to reply in, so the result goes to the admin by direct message.
  private async notifyAdmin(text: string): Promise<void> {
    try { await (await this.client.users.fetch(this.settings.adminId)).send(text); }
    catch (error) { console.error('Failed to notify the admin', { adminId: this.settings.adminId, text, error }); }
  }
  async runWeekly(force = false): Promise<SyncSummary | null> {
    if (!force && !this.settings.weekly) return null;
    if (!this.settings.channelId || !this.client.user) throw new Error('Weekly sync requires a configured channel and connected Discord client');
    const channel = await this.client.channels.fetch(this.settings.channelId);
    if (!channel || !channel.isSendable() || !(channel.type === ChannelType.GuildText || channel.type === ChannelType.GuildAnnouncement || channel.isThread())) throw new Error(`Weekly sync requires a guild text channel or thread: channel_id=${this.settings.channelId}`);
    const now = new Date();
    return syncChannelHistory(this.database, this.processor, discordHistory(channel), this.client.user.id, this.settings.batchLimit, this.settings.readOnly, { slot: weeklySlot(now), startedAt: now, force });
  }
  private async manualSync(channel: SendableChannels): Promise<void> {
    if (!this.client.user) throw new Error('Discord client is not connected');
    await channel.send('過去メッセージの同期を開始します。');
    try {
      const result = await syncChannelHistory(this.database, this.processor, discordHistory(channel), this.client.user.id, this.settings.batchLimit, this.settings.readOnly);
      if (!result) throw new Error('Manual channel sync returned no result');
      await channel.send(syncReport('同期', result));
    } catch (error) {
      console.error('Manual history sync failed', { channelId: channel.id, error });
      if (error instanceof SyncAlreadyRunning) await channel.send('このチャンネルは同期中です。完了後に再実行してください。');
      else if (error instanceof HistorySyncError) await channel.send(`同期を停止しました。${error.failed.length}件が続けて失敗しました。失敗した投稿は次回の同期で再試行します。message_id=${error.failed.join(', ')}`);
      else await channel.send(`同期処理に失敗しました。channel_id=${channel.id}`);
    }
  }
  private async message(message: Message): Promise<void> {
    const self = this.client.user;
    if (!self || message.author.id === self.id || !message.mentions.users.has(self.id)) return;
    if (message.author.id !== this.settings.adminId) { console.warn('Unauthorized bot command', { authorId: message.author.id, messageId: message.id }); return; }
    if (message.content.replaceAll(`<@${self.id}>`, '').trim().toLowerCase() === 'sync') {
      if (!message.channel.isSendable()) throw new Error(`Channel cannot receive Bot replies: channel_id=${message.channelId}`);
      await this.manualSync(message.channel); return;
    }
    await message.react('⏳');
    try {
      const existing = await this.database.query('SELECT processing_status FROM messages WHERE message_id=$1', z.object({ processing_status: z.string() }), [message.id]);
      if (existing[0] && ['succeeded', 'ignored'].includes(existing[0].processing_status)) { await message.react('👀'); return; }
      const result = await this.processor.processMessage(await acquireMessageEnvelope(discordInput(message), this.processor));
      if (result.ignored) { await message.react('⏭️'); return; }
      const lines = [`🍽️ ${result.shop_ids.length} 件を処理しました。`, result.pending_mention_ids.length ? `🔎 データ確認: ${result.pending_mention_ids.length} 件` : '✅ すべて強い根拠で確定しました。'];
      await message.reply(lines.join('\n'));
      await message.react(result.pending_mention_ids.length ? '🔎' : '✅');
    } catch (error) {
      console.error('Message processing failed', { messageId: message.id, error });
      await message.react('❌');
      await message.reply(`処理に失敗しました。message_id=${message.id}。同じ投稿でもう一度実行できます。`);
    } finally {
      try { await message.reactions.cache.find(reaction => reaction.emoji.name === '⏳')?.users.remove(self.id); }
      catch (error) { console.warn('Failed to remove progress reaction', { messageId: message.id, error }); }
    }
  }
}
