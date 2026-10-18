import { useState } from 'react';
import { CategorySelect } from './CategorySelect';
import type { ReactNode } from 'react';
import { z } from 'zod';
import { ApiError, api, csrf, detailSchema, editorOptionsSchema } from './api';
import type { Session, ShopDetail } from './api';
import { ErrorBox, Link, RatingInput, Skeleton, navigate, useResource } from './shared';

const fields = ['shop_name', 'branch_name', 'area', 'category', 'url', 'address', 'phone', 'memo', 'rating', 'visited_at'] as const;
type Field = typeof fields[number];
type Draft = Record<Field, string>;
const labels: Record<Field, string> = { shop_name: '店名', branch_name: '支店名', area: 'エリア', category: 'カテゴリ', url: '店舗URL', address: '住所', phone: '電話番号', memo: 'メモ', rating: '評価', visited_at: '訪問日' };
function values(shop: ShopDetail): Draft {
  return { shop_name: shop.name, branch_name: shop.branch ?? '', area: shop.area ?? '', category: shop.category ?? '', url: shop.canonical_url ?? '', address: shop.address ?? '', phone: shop.phone ?? '', memo: shop.memo, rating: shop.rating ? String(shop.rating) : '', visited_at: shop.visited_at?.slice(0, 10) ?? '' };
}
function Editor({ shop, session }: { shop: ShopDetail; session: Session }): ReactNode {
  const [draft, setDraft] = useState<Draft>(() => values(shop));
  const options = useResource('/api/ui/editor-options', editorOptionsSchema);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [latest, setLatest] = useState<ShopDetail | null>(null);
  const [choices, setChoices] = useState<Partial<Record<Field, 'input' | 'latest'>>>({});
  const latestValues = latest ? values(latest) : null;
  const differences = latestValues ? fields.filter(field => latestValues[field] !== draft[field]) : [];
  const submit = async (form: HTMLFormElement): Promise<void> => {
    setBusy(true); setError(null);
    const data = new FormData(form);
    data.set('rating', draft.rating); data.set('csrf_token', csrf()); data.set('expected_version', String(shop.version)); data.set('return_to', '/');
    if (draft.rating) data.set('is_visited', 'on');
    if (latest) {
      data.set('confirmed_version', String(latest.version)); data.set('confirm_conflict', 'on');
      for (const field of fields) data.append('conflict_choices', `${field}:${choices[field] ?? 'input'}`);
      data.append('conflict_choices', 'is_visited:input');
    }
    try {
      const result = await api(`/shop/${shop.id}/edit`, z.object({ redirect_url: z.string() }), { method: 'POST', body: data, headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      navigate(result.redirect_url);
    } catch (failure) {
      if (failure instanceof Error) setError(failure);
      if (failure instanceof ApiError && failure.status === 409) {
        try { setLatest(await api(`/api/ui/shops/${shop.id}`, detailSchema)); setChoices({}); }
        catch (readError) { console.error('Conflict snapshot failed', readError); }
      }
    } finally { setBusy(false); }
  };
  return <main className="main-shell editor-page"><div className="page-heading"><h1>店舗編集</h1><Link href={`/shop/${shop.id}`} className="text-button">戻る</Link></div><ErrorBox error={error} /><ErrorBox error={options.error} retry={options.reload} /><form className="edit-form" onSubmit={event => { event.preventDefault(); void submit(event.currentTarget); }}><fieldset disabled={busy || session.read_only}>{fields.filter(field => field !== 'visited_at').map(field => field === 'rating' ? <RatingInput key={field} rating={draft.rating ? Number(draft.rating) : null} onChange={rating => setDraft({ ...draft, rating: rating === null ? '' : String(rating) })} /> : <label className={field === 'memo' || field === 'address' ? 'wide-field' : ''} key={field}>{labels[field]}{field === 'memo' ? <textarea name={field} rows={4} maxLength={20000} value={draft[field]} onChange={event => setDraft({ ...draft, [field]: event.target.value })} /> : field === 'category' ? <CategorySelect name={field} value={draft.category} groups={options.data?.category_groups ?? []} retainedValue={shop.category} emptyLabel="未分類" disabled={!options.data} onChange={category => setDraft({ ...draft, category })} /> : <input name={field} value={draft[field]} required={field === 'shop_name'} maxLength={field === 'branch_name' ? 255 : undefined} type={field === 'url' ? 'url' : field === 'phone' ? 'tel' : 'text'} list={field === 'area' ? 'editor-areas' : undefined} onChange={event => setDraft({ ...draft, [field]: event.target.value })} />}</label>)}<input type="hidden" name="visited_at" value={draft.visited_at} /><datalist id="editor-areas">{options.data?.areas.map(area => <option key={area.value} value={area.value}>{area.label}</option>)}</datalist><label className="wide-field">写真<input type="file" name="photo" accept="image/jpeg,image/png,image/webp" /></label>{latestValues && <section className="wide-field conflict-panel"><h2>更新内容の確認</h2><p>入力内容を保持しています。項目ごとに保存する値を選択してください。</p>{differences.map(field => <fieldset key={field}><legend>{labels[field]}</legend><label><input type="radio" name={`choice-${field}`} checked={choices[field] === 'input'} onChange={() => setChoices({ ...choices, [field]: 'input' })} />入力：<span className="preserve-lines">{draft[field] || '（空欄）'}</span></label><label><input type="radio" name={`choice-${field}`} checked={choices[field] === 'latest'} onChange={() => setChoices({ ...choices, [field]: 'latest' })} />最新：<span className="preserve-lines">{latestValues[field] || '（空欄）'}</span></label></fieldset>)}</section>}<div className="form-actions wide-field"><Link href={`/shop/${shop.id}`} className="text-button">キャンセル</Link><button className="primary-button" disabled={!options.data || Boolean(latest && differences.some(field => !choices[field]))}>保存</button></div></fieldset></form><section className="danger-zone"><button className="text-button danger" disabled={busy || session.read_only} onClick={() => {
    if (!window.confirm(`「${shop.name}」と保存済みの写真・評価・メモを削除します。元投稿と確認履歴は残ります。削除しますか？`)) return;
    setBusy(true);
    api(`/api/ui/admin/shops/${shop.id}/delete`, z.object({ ok: z.boolean() }), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version: shop.version }) }).then(() => navigate('/'), (failure: Error) => { setError(failure); setBusy(false); });
  }}>店舗削除</button></section></main>;
}
export function ShopEditor({ id, session }: { id: number; session: Session }): ReactNode {
  const resource = useResource(`/api/ui/shops/${id}`, detailSchema);
  if (!session.is_admin) return <main className="main-shell"><ErrorBox error="管理者権限が必要です。" /></main>;
  if (resource.loading) return <Skeleton />;
  return resource.data ? <Editor key={resource.data.id} shop={resource.data} session={session} /> : <main className="main-shell"><ErrorBox error={resource.error} retry={resource.reload} /></main>;
}
