import { TextInput } from '@astryxdesign/core/TextInput';
import { TextArea } from '@astryxdesign/core/TextArea';
import { Selector } from '@astryxdesign/core/Selector';
import { CheckboxInput } from '@astryxdesign/core/CheckboxInput';
import { RadioList, RadioListItem } from '@astryxdesign/core/RadioList';
import type { InputStatus } from '@astryxdesign/core/Field';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Card } from '@astryxdesign/core/Card';
import { Collapsible } from '@astryxdesign/core/Collapsible';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { FormLayout } from '@astryxdesign/core/FormLayout';
import { Grid, GridSpan } from '@astryxdesign/core/Grid';
import { HStack } from '@astryxdesign/core/HStack';
import { List, ListItem } from '@astryxdesign/core/List';
import { Spinner } from '@astryxdesign/core/Spinner';
import { VStack } from '@astryxdesign/core/VStack';
import { AreaInput, blockComboboxSubmit } from './ui';
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useToast } from '@astryxdesign/core/Toast';
import { CategorySelect } from './CategorySelect';
import type { ReactNode } from 'react';
import { z } from 'zod';
import { ApiError, api, decisionSchema, editorOptionsSchema, jsonBody, linkPreviewSchema, reviewSchema, reviewShopSchema, reviewsSchema, suggestionSchema } from './api';
import type { Review, Session, Suggestion } from './api';
import { ErrorBox, ExternalLink, Modal, Skeleton, closeDialog, navigate, openDialog, replaceDialog, useResource } from './shared';

const fieldNames = ['shop_name', 'branch_name', 'area', 'category', 'address', 'phone', 'canonical_url'] as const;
type Field = typeof fieldNames[number];
type Draft = Record<Field, string>;
interface ReviewDraft { fields: Draft; note: string; reference: string; photo: number | null; suggestions: Suggestion[]; selectedSuggestion: Suggestion | null; }
const labels: Record<Field, string> = { shop_name: '店名', branch_name: '支店名', area: 'エリア', category: 'カテゴリ', address: '住所', phone: '電話番号', canonical_url: '店舗URL' };
function savedValues(item: Review): Draft {
  return { shop_name: item.shop?.shop_name ?? item.extracted_name, branch_name: item.shop?.branch_name ?? item.extracted_branch_name ?? '', area: item.shop?.area ?? item.extracted_area ?? '', category: item.shop?.category ?? item.extracted_category ?? '', address: item.shop?.address ?? '', phone: item.shop?.phone ?? '', canonical_url: item.shop?.canonical_url ?? '' };
}
type LinkPreview = z.infer<typeof linkPreviewSchema>;
type MergeTarget = z.infer<typeof reviewShopSchema>;
function ReviewEditor({ item, session, scope, initial, remember, done, refresh, footerSlot }: { item: Review; session: Session; scope: string; initial?: ReviewDraft; remember: (draft: ReviewDraft) => void; done: () => void; refresh: () => void; footerSlot: HTMLElement | null }): ReactNode {
  const [draft, setDraft] = useState<Draft>(initial?.fields ?? savedValues(item));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [note, setNote] = useState(initial?.note ?? '');
  const [reference, setReference] = useState(initial?.reference ?? '');
  const [photo, setPhoto] = useState<number | null>(initial?.photo ?? null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>(initial?.suggestions ?? []);
  const [selectedSuggestion, setSelectedSuggestion] = useState<Suggestion | null>(initial?.selectedSuggestion ?? null);
  const [notice, setNotice] = useState('');
  const [targetId, setTargetId] = useState('');
  const [linkPreview, setLinkPreview] = useState<LinkPreview | null>(null);
  const [mergeTarget, setMergeTarget] = useState<MergeTarget | null>(null);
  const [mergeMemo, setMergeMemo] = useState('');
  const [mergeRating, setMergeRating] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  const [invalid, setInvalid] = useState<Partial<Record<Field, string>>>({});
  const fieldStatus = (field: Field): InputStatus | undefined => invalid[field] ? { type: 'error', message: invalid[field] } : undefined;
  const options = useResource('/api/ui/editor-options', editorOptionsSchema);
  const effectiveScope = scope === 'metadata' || (scope === 'all' && item.review_status === 'approved') ? 'metadata' : 'identity';
  useEffect(() => { remember({ fields: draft, note, reference, photo, suggestions, selectedSuggestion }); }, [draft, note, reference, photo, suggestions, selectedSuggestion, remember]);
  const run = async (operation: () => Promise<void>): Promise<void> => {
    setBusy(true); setError(null); setNotice('');
    try { await operation(); }
    catch (failure) { console.error('Review operation failed', { mentionId: item.id, failure }); if (failure instanceof Error) setError(failure); if (failure instanceof ApiError && failure.status === 409) { setConflicted(true); setAcknowledged(false); } }
    finally { setBusy(false); }
  };
  const decision = async (body: object): Promise<void> => {
    await api(`/api/admin/reviews/${item.id}/decision`, decisionSchema, jsonBody({ ...body, expected_version: item.version, shop_version: item.shop?.version ?? null, scope: effectiveScope })); done();
  };
  const disabled = busy || session.read_only || (conflicted && !acknowledged);
  const [rejecting, setRejecting] = useState(false);
  const photos = item.assets.filter(asset => asset.photo_url);
  const stored = savedValues(item);
  const dirty = fieldNames.some(field => draft[field] !== stored[field]) || photo !== null || selectedSuggestion !== null;
  // The decision buttons sit in the footer, so bring an error into view when one appears.
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (error) errorRef.current?.scrollIntoView({ block: 'nearest' }); }, [error]);
  return <>{error && <div ref={errorRef}><ErrorBox error={error} /></div>}<ErrorBox error={options.error} retry={options.reload} />{notice && <Banner status="info" title={notice} />}{conflicted && <Card className="conflict-panel" padding={4}><VStack gap={3}><Banner status="warning" title="入力内容を保持しています。最新の店舗情報と比較してから保存してください。" endContent={<Button variant="ghost" size="sm" label="最新情報を読み込む" onClick={refresh} />} /><pre>{JSON.stringify(item.shop, null, 2)}</pre><CheckboxInput label="最新の内容と入力を確認しました" value={acknowledged} onChange={setAcknowledged} /></VStack></Card>}<Card className="review-evidence" padding={4}><h3>判断材料・元投稿</h3><p className="preserve-lines">{item.message_content}</p><ExternalLink href={item.source_url}>出典</ExternalLink><ExternalLink href={item.discord_url}>Discord</ExternalLink>{item.issues.map(issue => <p key={issue.code}>{issue.label}</p>)}{item.confidence_reason && <p>{item.confidence_reason}</p>}{item.extraction_error && <p className="field-error">{item.extraction_error}</p>}{item.review_group_count && item.review_group_count > 1 && <p>関連する確認項目：{item.review_group_count}件 · {item.review_group_reason}</p>}{item.assets.filter(asset => asset.extracted_text).map(asset => <p className="preserve-lines" key={asset.id}>{asset.extracted_text}</p>)}</Card>{photos.length > 0 && <RadioList containerClassName="review-photos" label="写真" value={photo === null ? '' : String(photo)} onChange={value => setPhoto(value ? Number(value) : null)}><RadioListItem value="" label="変更しない" />{photos.map(asset => <RadioListItem key={asset.id} value={String(asset.id)} isDisabled={disabled} aria-label={asset.title ?? '投稿の写真'} label={<img src={asset.photo_url ?? ''} alt={asset.title ?? '投稿の写真'} loading="lazy" />} />)}</RadioList>}<Card padding={4}><form id="review-edit-form" className="review-edit-form" onKeyDown={blockComboboxSubmit} onSubmit={event => { event.preventDefault(); const problems: Partial<Record<Field, string>> = {}; if (!draft.shop_name.trim()) problems.shop_name = '店名を入力してください。'; if (!draft.area.trim()) problems.area = 'エリアを入力してください。'; if (!draft.category.trim()) problems.category = 'カテゴリを選択してください。'; setInvalid(problems); if (Object.keys(problems).length) { event.currentTarget.scrollIntoView({ block: 'start' }); return; } void run(() => decision({ action: 'edit_and_approve', confirm_metadata: scope === 'all', photo_asset_id: photo, shop: { ...draft, branch_name: draft.branch_name || null, address: draft.address || null, phone: draft.phone || null, canonical_url: draft.canonical_url || null }, supplement: note || null, suggestion_evidence_urls: selectedSuggestion?.evidence_urls ?? [], suggestion_reason: selectedSuggestion?.reason ?? null })); }}><h3>店舗情報</h3><FormLayout><Grid columns={{ minWidth: 220, max: 2 }} gap={4}>{fieldNames.map(field => <GridSpan key={field}>{field === 'category' ? <CategorySelect label={labels[field]} value={draft.category} groups={options.data?.category_groups ?? []} retainedValue={savedValues(item).category} emptyLabel="未分類" disabled={disabled || !options.data} required status={fieldStatus(field)} onChange={category => setDraft(previous => ({ ...previous, category }))} /> : field === 'area' ? <AreaInput label={labels[field]} value={draft.area} areas={options.data?.areas ?? []} required disabled={disabled || !options.data} status={fieldStatus(field)} onChange={area => setDraft(previous => ({ ...previous, area }))} /> : <TextInput label={labels[field]} isRequired={field === 'shop_name'} value={draft[field]} type={field === 'canonical_url' ? 'url' : 'text'} isDisabled={disabled} status={fieldStatus(field)} onChange={value => setDraft(previous => ({ ...previous, [field]: value }))} />}</GridSpan>)}</Grid></FormLayout></form></Card><VStack className="admin-section" gap={3}><TextArea label="補足" description="保留するとき・採用しないときは、理由として保存されます。" value={note} maxLength={4000} onChange={setNote} /><TextInput label="参照URL" type="url" value={reference} onChange={setReference} /><Button variant="secondary" label="AIで再読み込み" isDisabled={disabled || fieldNames.every(field => draft[field].trim())} onClick={() => void run(async () => {
    const result = await api(`/api/admin/reviews/${item.id}/suggestions`, suggestionSchema, jsonBody({ expected_version: item.version, shop_version: item.shop?.version ?? null, draft, supplement: note, reference_url: reference || null })); setSuggestions(result.candidates); if (!result.candidates.length) setNotice(result.unresolved_reason ?? '候補がありません。');
  })} />{suggestions.map((suggestion, index) => <Card className="candidate" padding={3} key={index}><p>{suggestion.shop_name} / {suggestion.area}</p><p>{suggestion.reason}</p>{suggestion.evidence_urls.map(url => <ExternalLink key={url} href={url}>出典</ExternalLink>)}<Button variant="ghost" size="sm" label="空欄へ反映" isDisabled={disabled} onClick={() => { setDraft(previous => { const next = { ...previous }; for (const field of fieldNames) if (!next[field].trim()) next[field] = suggestion[field] ?? ''; return next; }); setSelectedSuggestion(suggestion); }} /></Card>)}</VStack>{item.candidates.length > 0 && <VStack className="admin-section" gap={3}><h3>候補</h3>{item.candidates.map(candidate => <Card className="candidate" padding={3} key={candidate.id}><p>{candidate.name} / {candidate.area} / {candidate.category}</p><p>{candidate.verification_reason}</p>{candidate.conflicting_fields && <p className="field-error">{candidate.conflicting_fields}</p>}<ExternalLink href={candidate.evidence_url ?? candidate.canonical_url}>出典</ExternalLink><Button variant="ghost" size="sm" label="候補を承認" isDisabled={disabled} onClick={() => void run(() => decision({ action: 'approve_candidate', candidate_id: candidate.id }))} /></Card>)}</VStack>}{effectiveScope === 'identity' && <Card className="admin-section" padding={4}><Collapsible trigger="関連付け・統合" defaultIsOpen={false}><VStack className="collapsible-body" gap={3}><TextInput label="店舗ID" inputMode="numeric" value={targetId} onChange={value => { setTargetId(value); setLinkPreview(null); setMergeTarget(null); }} />{effectiveScope === 'identity' && <Button variant="secondary" label="関連付けを確認" isDisabled={disabled || !/^[1-9]\d*$/.test(targetId) || !note.trim()} onClick={() => void run(async () => { setLinkPreview(await api(`/api/admin/reviews/${item.id}/link-preview?target_shop_id=${targetId}`, linkPreviewSchema)); })} />}{linkPreview && <Card className="conflict-panel" padding={4}><p>{linkPreview.source_shop?.shop_name ?? '未関連付け'} → {linkPreview.target_shop.shop_name}</p><p>元の店舗：{linkPreview.source_mention_count}件 / 関連付け先：{linkPreview.target_mention_count}件</p>{linkPreview.source_will_be_empty && <p>元の店舗から投稿の関連付けがなくなります。</p>}<p>この確認項目1件の関連付けを変更します。店舗の評価・メモは保持します。</p><Button variant="primary" label="関連付け" isDisabled={disabled} onClick={() => void run(async () => { await api(`/api/admin/reviews/${item.id}/link`, decisionSchema, jsonBody({ expected_version: linkPreview.expected_version, source_shop_id: linkPreview.source_shop?.id ?? null, source_shop_version: linkPreview.source_shop?.version ?? null, target_shop_id: linkPreview.target_shop.id, target_shop_version: linkPreview.target_shop.version, note })); done(); })} /></Card>}<Button variant="secondary" label="統合先を確認" isDisabled={disabled || !/^[1-9]\d*$/.test(targetId)} onClick={() => void run(async () => { const result = await api(`/api/admin/shops/${targetId}/merge-preview`, reviewShopSchema); setMergeTarget(result); setMergeMemo(result.memo ?? ''); setMergeRating(result.rating ? String(result.rating) : ''); })} />{mergeTarget && <Card className="conflict-panel" padding={4}><VStack gap={3}><p>統合先：{mergeTarget.shop_name} / {mergeTarget.area}</p><Selector label="評価" options={[{ value: '', label: '未評価' }, ...[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: `${value}/5` }))]} value={mergeRating} onChange={setMergeRating} width="100%" /><TextArea label="メモ" value={mergeMemo} onChange={setMergeMemo} maxLength={20000} /><Banner status="warning" title="統合すると元の店舗は削除されます" description="元店舗のすべての関連付けを統合先へ移します。統合先の評価・メモは上の入力内容で更新します。画面からは元に戻せません。" /><Button variant="destructive" label="統合を実行" isDisabled={disabled} onClick={() => void run(() => decision({ action: 'merge', target_shop_id: mergeTarget.id, target_version: mergeTarget.version, rating: mergeRating ? Number(mergeRating) : null, is_visited: Boolean(mergeRating), visited_at: mergeTarget.visited_at, memo: mergeMemo }))} /></VStack></Card>}</VStack></Collapsible></Card>}{footerSlot && createPortal(rejecting && item.shop ? <VStack gap={2} width="100%"><p className="review-footer-note">この投稿を採用しません。関連付けた店舗「{item.shop.shop_name}」の扱いを選んでください。「店舗も削除する」を選ぶと、ほかに投稿がない店舗は評価・メモごと消え、元に戻せません。</p><HStack justify="end" gap={2} wrap="wrap"><Button variant="ghost" label="キャンセル" isDisabled={busy} onClick={() => setRejecting(false)} /><Button variant="secondary" label="店舗は残す" isDisabled={disabled} onClick={() => void run(() => decision({ action: 'exclude', reason: note || null }))} /><Button variant="destructive" label="店舗も削除する" isDisabled={disabled} onClick={() => void run(() => decision({ action: 'reject', reason: note || null }))} /></HStack></VStack> : <VStack gap={2} width="100%">{dirty && <HStack justify="end" align="center" gap={2} wrap="wrap"><span className="review-footer-note">入力した変更は「保存・承認」で保存されます。</span><Button variant="ghost" size="sm" label="入力を元に戻す" isDisabled={disabled} onClick={() => { setDraft(stored); setPhoto(null); setSelectedSuggestion(null); setInvalid({}); }} /></HStack>}<HStack justify="between" align="center" gap={3} wrap="wrap" width="100%">{effectiveScope === 'identity' ? <Button variant="secondary" label="採用しない" isDisabled={disabled} onClick={() => { if (item.shop) setRejecting(true); else void run(() => decision({ action: 'reject', reason: note || null })); }} /> : <span className="review-footer-note">エリア・カテゴリの確認中です。「採用しない」は、範囲「店舗」で開くと使えます。</span>}<HStack gap={2} wrap="wrap" justify="end"><Button variant="secondary" label="保留" isDisabled={disabled} onClick={() => void run(() => decision({ action: 'defer', note: note || null }))} /><Button variant="secondary" label="現在の内容で承認" isDisabled={disabled || dirty} onClick={() => void run(() => decision({ action: 'approve_current' }))} /><Button type="submit" form="review-edit-form" variant="primary" label="保存・承認" isDisabled={disabled || !options.data} /></HStack></HStack></VStack>, footerSlot)}</>;
}
function ReviewDetail({ id, session, scope, draft, remember, close, saved }: { id: number; session: Session; scope: string; draft?: ReviewDraft; remember: (draft: ReviewDraft) => void; close: () => void; saved: () => void }): ReactNode {
  const resource = useResource(`/api/admin/reviews/${id}`, reviewSchema);
  const [footerSlot, setFooterSlot] = useState<HTMLDivElement | null>(null);
  // While the next item loads, the resource still holds the previous one; never show it under the new id.
  const item = resource.data?.id === id ? resource.data : null;
  return <Modal title="データ確認" close={close} detail footer={<div className="review-footer" ref={setFooterSlot} />}><div className="review-content">{resource.loading && !item && <Skeleton full={false} />}<ErrorBox error={resource.error} retry={resource.reload} />{item && <ReviewEditor key={id} item={item} session={session} scope={scope} initial={draft} remember={remember} done={saved} refresh={resource.reload} footerSlot={footerSlot} />}</div></Modal>;
}
export function Reviews({ session, location }: { session: Session; location: string }): ReactNode {
  const params = new URL(location, window.location.origin).searchParams;
  const query = new URLSearchParams(params); query.delete('mention'); if (!query.has('status')) query.set('status', 'unresolved');
  const resource = useResource('/api/admin/reviews?' + query.toString(), reviewsSchema);
  const [drafts, setDrafts] = useState<Map<number, ReviewDraft>>(new Map());
  const id = Number(params.get('mention')) || null;
  const toast = useToast();
  const remember = useCallback((draft: ReviewDraft): void => { if (id === null) return; setDrafts(previous => { if (JSON.stringify(previous.get(id)) === JSON.stringify(draft)) return previous; const next = new Map(previous); next.set(id, draft); return next; }); }, [id]);
  const change = (values: FormData): void => { const next = new URLSearchParams(); for (const name of ['q', 'scope', 'status', 'reason', 'source']) { const value = values.get(name); if (typeof value === 'string' && value) next.set(name, value); } navigate('/admin/review?' + next.toString()); };
  const close = (): void => closeDialog('/admin/review?' + query.toString());
  const queryKey = query.toString();
  const [filter, setFilter] = useState({ status: params.get('status') ?? 'unresolved', q: params.get('q') ?? '', scope: params.get('scope') ?? 'all', source: params.get('source') ?? '', reason: params.get('reason') ?? '' });
  // Keep the filter inputs in step with the URL, e.g. after browser back.
  useEffect(() => { const current = new URLSearchParams(queryKey); setFilter({ status: current.get('status') ?? 'unresolved', q: current.get('q') ?? '', scope: current.get('scope') ?? 'all', source: current.get('source') ?? '', reason: current.get('reason') ?? '' }); }, [queryKey]);
  const setFilterValue = (name: keyof typeof filter) => (value: string): void => setFilter(previous => ({ ...previous, [name]: value }));
  return <main className="admin-screen"><h1>データ確認 <span className="muted">{resource.data?.total_count ?? ''}</span></h1><Card padding={4}><form className="review-filter" onSubmit={event => { event.preventDefault(); change(new FormData(event.currentTarget)); }}><Selector label="状態" htmlName="status" options={[{ value: 'unresolved', label: '未確認' }, { value: 'pending', label: '確認待ち' }, { value: 'deferred', label: '保留' }, { value: 'approved', label: '承認済み' }, { value: 'rejected', label: '不採用' }]} value={filter.status} onChange={setFilterValue('status')} /><TextInput label="検索" htmlName="q" value={filter.q} onChange={setFilterValue('q')} /><Button type="submit" variant="primary" label="検索" /><div className="review-extra"><Collapsible trigger="追加条件" defaultIsOpen={false}><div className="review-extra-fields collapsible-body"><Selector label="範囲" htmlName="scope" options={[{ value: 'all', label: 'すべて' }, { value: 'identity', label: '店舗' }, { value: 'metadata', label: 'エリア・カテゴリ' }]} value={filter.scope} onChange={setFilterValue('scope')} /><TextInput label="出典" htmlName="source" value={filter.source} onChange={setFilterValue('source')} /><TextInput label="理由" htmlName="reason" value={filter.reason} onChange={setFilterValue('reason')} /></div></Collapsible></div></form></Card><ErrorBox error={resource.error} retry={resource.reload} />{resource.loading && <Spinner label="読み込み中" />}{resource.data && resource.data.items.length > 0 && <Card className="review-list" padding={2}><List hasDividers>{resource.data.items.map(item => <ListItem key={item.id} label={item.shop?.shop_name ?? item.extracted_name} description={<><span>{item.shop?.area ?? item.extracted_area} / {item.shop?.category ?? item.extracted_category}</span><br /><span>{item.issues.map(issue => issue.label).join('・')}</span></>} onClick={() => openDialog('/admin/review?' + new URLSearchParams({ ...Object.fromEntries(query), mention: String(item.id) }).toString())} />)}</List></Card>}{resource.data?.items.length === 0 && <EmptyState title="確認項目はありません" isCompact />}<HStack gap={3}>{resource.data?.next_cursor && <Button variant="ghost" label="次へ" onClick={() => { const next = new URLSearchParams(query); next.set('cursor', String(resource.data?.next_cursor)); navigate('/admin/review?' + next.toString()); }} />}{params.has('cursor') && <Button variant="ghost" label="先頭" onClick={() => { const next = new URLSearchParams(query); next.delete('cursor'); navigate('/admin/review?' + next.toString()); }} />}</HStack>{id && <ReviewDetail id={id} session={session} scope={query.get('scope') ?? 'all'} draft={drafts.get(id)} remember={remember} close={close} saved={() => {
    setDrafts(previous => { const next = new Map(previous); next.delete(id); return next; });
    // Open the next item in the list so a batch can be worked through without reopening.
    const items = resource.data?.items ?? [];
    const following = items[items.findIndex(item => item.id === id) + 1];
    if (following && following.id !== id) { replaceDialog('/admin/review?' + new URLSearchParams({ ...Object.fromEntries(query), mention: String(following.id) }).toString()); toast({ body: '処理しました。次の確認項目を表示しています。' }); }
    else { close(); toast({ body: '処理しました。' }); }
    resource.reload();
  }} />}</main>;
}
