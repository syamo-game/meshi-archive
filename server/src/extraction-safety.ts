import type { ExtractedMention, ImageClues } from './ai-models.js';
import { casefold, escapeRegex } from './text.js';

export type EvidenceCode = 'input_incomplete' | 'identity_unclear' | 'name_ungrounded' | 'branch_ungrounded' | 'non_store_subject' | 'event_excluded';
export interface EvidenceAssessment { codes: readonly EvidenceCode[]; reasons: readonly string[]; }
const empty: EvidenceAssessment = { codes: [], reasons: [] };
const authorLine = /^\[Embed Author\]|^\[Embed Title\].*(?:@|\bon [Xx]\b)/;
const nameCharacters = '0-9a-zぁ-んァ-ヶ一-龠ー';
function normalized(value: string): string { return casefold(value.normalize('NFKC')).replace(/\s+/gu, ' ').trim(); }
function nonAuthorContent(content: string): string { return content.split(/\r\n|[\n\r\u0085\u2028\u2029]/).filter(line => !authorLine.test(line.trim()) && !line.trimStart().startsWith('[Attachment]')).join('\n'); }
function unique(codes: readonly EvidenceCode[], reasons: readonly string[]): EvidenceAssessment { return { codes: [...new Set(codes)], reasons: [...new Set(reasons)] }; }
const nonStoreReason = (subject: 'event' | 'product'): string => subject === 'event' ? '催事は登録対象外です。出店元の常設店舗を特定できないため、会場や仮設支店は登録しません。' : '商品への言及を常設店舗として自動登録しません。販売店との関係を確認してください。';
export function sourceRequiresFreshExtraction(content: string): boolean { return /商品名|舟盛り|催事|物産展|期間限定|ポップアップ|pop.?up|出店|閉店|営業終了/i.test(content); }
function explicitNonStoreSubject(name: string, content: string): 'product' | 'event' | null {
  const quoted = escapeRegex(normalized(name)); const source = normalized(content);
  const product = new RegExp(`(?:商品名|舟盛り(?:商品)?|メニュー名)\\s*(?:は|[:：])?\\s*[「『]${quoted}[」』]|[「『]${quoted}[」』]\\s*(?:という|は|の)?\\s*(?:舟盛り|商品)`);
  const event = new RegExp(`[「『]?${quoted}[」』]?\\s*(?:が|の|は)?\\s*(?:期間限定出店|催事出店|物産展出店)|[「『]?${quoted}[」』]?(?:が|は|の)[^。！\\n]{0,45}(?:催事|物産展)[^。！\\n]{0,15}出店`);
  return product.test(source) ? 'product' : event.test(source) ? 'event' : null;
}
export function containsEvidenceName(name: string, text: string): boolean {
  const compact = normalized(name).replace(new RegExp(`[^${nameCharacters}]+`, 'g'), ''); if (!compact) return false;
  const pattern = new RegExp([...compact].map(escapeRegex).join(`[^${nameCharacters}]*`), 'g'); const source = normalized(text);
  for (const match of source.matchAll(pattern)) {
    const prefix = source.slice(0, match.index); const suffix = source.slice(match.index + match[0].length);
    const left = !prefix || !new RegExp(`[${nameCharacters}]$`).test(prefix) || /(?:店名は|店舗名は|お店は|店は|にある|の|昨日|今日|先日|さっき)$/.test(prefix);
    const right = !suffix || !new RegExp(`^[${nameCharacters}]`).test(suffix) || (!/^(?:はなれ|別館|本店|支店)/.test(suffix) && /^(?:へ|で|を|に|は|が|の|と)/.test(suffix));
    if (left && right) return true;
  }
  return false;
}
export function inputEvidenceAssessment(content: string, omittedAssets = 0, sourceTruncated = false): EvidenceAssessment {
  const reasons: string[] = [];
  if (/(?:続きを読む|続きを(?:読む|見る)|(?:…|\.\.\.)\s*続き(?:はこちら)?)\s*[）)」]*\s*$/m.test(content)) reasons.push('本文に続きの省略を示す表示があります。全文を確認してください。');
  if (omittedAssets) reasons.push(`添付等${omittedAssets}件が解析対象の上限を超えています。`);
  if (sourceTruncated) reasons.push('出典の本文または件数が検索入力の上限を超えています。');
  return { codes: reasons.length ? ['input_incomplete'] : [], reasons };
}
export function assessMentionEvidence(mention: ExtractedMention, content: string, input: EvidenceAssessment = empty, sourceGrounded = false): EvidenceAssessment {
  const codes = [...input.codes]; const reasons = [...input.reasons];
  let subject = mention.subject_kind === 'product' || mention.subject_kind === 'event' ? mention.subject_kind : explicitNonStoreSubject(mention.shop_name, content);
  if (subject === 'event' && mention.subject_kind === 'restaurant' && mention.identity_evidence === 'explicit' && mention.name_evidence && normalized(content).includes(normalized(mention.name_evidence)) && /常設|実店舗|本店|店舗所在地|営業時間/.test(mention.name_evidence) && explicitNonStoreSubject(mention.shop_name, mention.name_evidence) === null) subject = null;
  if (subject) { codes.push('non_store_subject'); if (subject === 'event') codes.push('event_excluded'); reasons.push(nonStoreReason(subject)); }
  if (['ambiguous', 'author_only', 'unknown'].includes(mention.identity_evidence ?? '') || mention.subject_kind === 'unknown') { codes.push('identity_unclear'); reasons.push('店名の根拠が曖昧か投稿者名だけです。対象店舗を確認してください。'); }
  if (mention.identity_evidence === 'explicit' && !sourceGrounded) {
    const source = normalized(nonAuthorContent(content)); const evidence = mention.name_evidence ?? '';
    if (!evidence || !source.includes(normalized(evidence)) || !containsEvidenceName(mention.shop_name, evidence)) { codes.push('name_ungrounded'); reasons.push('店名を裏付ける引用を投稿本文で確認できません。音写や推測で確定しません。'); }
    if (mention.branch_name) { const branch = mention.branch_evidence ?? ''; if (!branch || !source.includes(normalized(branch)) || !containsEvidenceName(mention.branch_name, branch)) { codes.push('branch_ungrounded'); reasons.push('支店を裏付ける引用がありません。所在地だけから支店名を補いません。'); } }
  }
  return unique(codes, reasons);
}
export function imageEvidenceAssessment(subject: ImageClues['subject_kind'], content = '', unresolved = '', input: EvidenceAssessment = empty): EvidenceAssessment {
  const context = content + '\n' + unresolved; const codes = [...input.codes]; const reasons = [...input.reasons];
  if (/(?:期間限定)?(?:催事|物産展)(?:の(?:紹介|チラシ)|紹介|出店)|期間限定出店/.test(context)) subject = 'event';
  else if (/(?:商品|舟盛り)(?:の(?:紹介|チラシ)|紹介)/.test(context)) subject = 'product';
  if (subject === 'product' || subject === 'event') { codes.push('non_store_subject'); if (subject === 'event') codes.push('event_excluded'); reasons.push(nonStoreReason(subject)); }
  else if (subject !== 'restaurant') { codes.push('identity_unclear'); reasons.push('画像の店名と商品・催事の区別が未確認です。対象店舗を確認してください。'); }
  return unique(codes, reasons);
}
export function eventExclusionAssessment(reason: string | null = null): EvidenceAssessment { return { codes: ['non_store_subject', 'event_excluded'], reasons: [nonStoreReason('event'), ...(reason ? [reason] : [])] }; }
interface OriginQuotes { name: string; branch: string | null; area: string | null; relation_evidence: string; permanent_evidence: string; relation_text: string; permanent_text: string; }
export function eventOriginQuotesMatch(input: OriginQuotes): boolean {
  if (input.branch && /催事|物産展|会場|期間限定|ポップアップ|pop.?up/i.test(input.branch)) return false;
  if (![[input.relation_evidence, input.relation_text], [input.permanent_evidence, input.permanent_text]].every(pair => normalized(nonAuthorContent(pair[1] ?? '')).includes(normalized(pair[0] ?? '')))) return false;
  const name = input.branch && !input.name.includes(input.branch) ? input.name + ' ' + input.branch : input.name;
  if (![input.relation_evidence, input.permanent_evidence].every(quote => containsEvidenceName(name, quote))) return false;
  if (input.area && !normalized(input.permanent_evidence).includes(normalized(input.area))) return false;
  if (/(?:常設|実店舗|本店|出店元).{0,16}(?:ありません|存在しない|なし|なく|持たず|不明)|ではなく|ではない|ではありません|未確認|(?:出店).{0,8}(?:しない|しません)|(?:旧店舗|移転前|閉鎖済みの会場)/.test(input.relation_evidence + '\n' + input.permanent_evidence)) return false;
  const namePattern = [...normalized(name).replaceAll(' ', '')].map(escapeRegex).join('\\s*');
  const direct = new RegExp(`${namePattern}[」』]?(?:が|から|より)[^。！\\n]{0,45}出店`).test(normalized(input.relation_evidence));
  return (/出店元|出店者|運営元/.test(input.relation_evidence) || direct) && /常設|実店舗|本店|店舗所在地|営業時間|定休日/.test(input.permanent_evidence);
}
export function evidenceReviewError(assessment: EvidenceAssessment): string | null { return assessment.codes.length ? 'evidence_review:' + JSON.stringify(assessment) : null; }
export function operatingStatusNote(mention: ExtractedMention): string | null {
  const labels = { open: '営業中', closed: '閉店', event_ended: '催事終了', unknown: null } as const;
  const label = mention.operating_status ? labels[mention.operating_status] : null;
  return label ? `営業状態の記述（店舗の同定とは別）: ${label} / 根拠: ${mention.operating_status_evidence || '根拠引用なし・要確認'}` : null;
}
