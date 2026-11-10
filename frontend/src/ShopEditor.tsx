import { Card } from '@astryxdesign/core/Card';
import './shop-editor.css';
import { Button } from '@astryxdesign/core/Button';
import { FileInput } from '@astryxdesign/core/FileInput';
import { RadioList, RadioListItem } from '@astryxdesign/core/RadioList';
import { TextArea } from '@astryxdesign/core/TextArea';
import { TextInput } from '@astryxdesign/core/TextInput';
import type { InputStatus } from '@astryxdesign/core/Field';
import { useState } from 'react';
import { CategorySelect } from './CategorySelect';
import { AreaInput, blockComboboxSubmit } from './ui';
import { Banner } from '@astryxdesign/core/Banner';
import { FormLayout } from '@astryxdesign/core/FormLayout';
import { Grid, GridSpan } from '@astryxdesign/core/Grid';
import { HStack } from '@astryxdesign/core/HStack';
import { VStack } from '@astryxdesign/core/VStack';
import type { ReactNode } from 'react';
import { z } from 'zod';
import { ApiError, api, csrf, detailSchema, editorOptionsSchema } from './api';
import type { Session, ShopDetail } from './api';
import { ErrorBox, RatingInput, Skeleton, navigate, useResource } from './shared';

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
  const [photo, setPhoto] = useState<File | null>(null);
  const [invalid, setInvalid] = useState<Partial<Record<Field, string>>>({});
  const submit = async (form: HTMLFormElement): Promise<void> => {
    // Astryx inputs do not set native required or maxLength, so check here.
    const problems: Partial<Record<Field, string>> = {};
    if (!draft.shop_name.trim()) problems.shop_name = '店名を入力してください。';
    if (draft.branch_name.length > 255) problems.branch_name = '支店名は255文字以内で入力してください。';
    setInvalid(problems);
    if (Object.keys(problems).length) return;
    setBusy(true); setError(null);
    const data = new FormData(form);
    for (const field of fields) if (field !== 'rating') data.set(field, draft[field]);
    if (photo) data.set('photo', photo);
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
  const update = (field: Field, value: string): void => setDraft(current => ({ ...current, [field]: value }));
  const storeUrl = `/shop/${shop.id}`;
  const blocked = busy || session.read_only;
  const [deleting, setDeleting] = useState(false);
  const textFields = ['shop_name', 'branch_name', 'area', 'category', 'url', 'phone', 'address', 'memo'] as const;
  return (
    <main className="main-shell shop-editor">
      <header className="shop-editor-heading"><h1>店舗編集</h1></header>
      <ErrorBox error={error} />
      <ErrorBox error={options.error} retry={options.reload} />
      <Card className="shop-editor-card" padding={5}>
        <form onKeyDown={blockComboboxSubmit} onSubmit={event => { event.preventDefault(); void submit(event.currentTarget); }}>
          <FormLayout>
            <Grid columns={{ minWidth: 260, max: 2 }} gap={4}>
              {textFields.map(field => {
                const status: InputStatus | undefined = invalid[field] ? { type: 'error', message: invalid[field] } : undefined;
                return <GridSpan columns={field === 'address' || field === 'memo' ? 'full' : 1} key={field}>
                  {field === 'category' ? <CategorySelect label={labels[field]} name={field} value={draft.category} groups={options.data?.category_groups ?? []} retainedValue={shop.category} emptyLabel="未分類" disabled={blocked || !options.data} onChange={value => update(field, value)} />
                    : field === 'area' ? <AreaInput label={labels[field]} value={draft.area} areas={options.data?.areas ?? []} disabled={blocked || !options.data} onChange={value => update(field, value)} />
                    : field === 'memo' ? <TextArea label={labels[field]} htmlName={field} rows={6} maxLength={20000} value={draft[field]} isDisabled={blocked} onChange={value => update(field, value)} />
                    : <TextInput label={labels[field]} htmlName={field} type={field === 'url' ? 'url' : field === 'phone' ? 'tel' : 'text'} isRequired={field === 'shop_name'} value={draft[field]} isDisabled={blocked} status={status} onChange={value => update(field, value)} />}
                </GridSpan>;
              })}
              <GridSpan columns="full"><RatingInput rating={draft.rating ? Number(draft.rating) : null} disabled={blocked} onChange={rating => update('rating', rating === null ? '' : String(rating))} /></GridSpan>
              <GridSpan columns="full"><VStack gap={2}><FileInput label="写真" mode="input" accept="image/jpeg,image/png,image/webp" description={shop.image_url ? '新しい写真を選ぶと、現在の写真と置き換わります。' : undefined} value={photo} isDisabled={blocked} onChange={selection => setPhoto(Array.isArray(selection) ? selection[0] ?? null : selection)} />{shop.image_url && <figure className="shop-editor-photo"><img src={shop.image_url} alt="" /><figcaption>現在の写真</figcaption></figure>}</VStack></GridSpan>
              {latestValues && <GridSpan columns="full"><VStack gap={3}>
                <Banner status="warning" title="更新内容の確認" description="入力内容を保持しています。項目ごとに保存する値を選択してください。" />
                {differences.map(field => <RadioList key={field} label={labels[field]} value={choices[field] ?? ''} onChange={choice => setChoices({ ...choices, [field]: choice === 'latest' ? 'latest' : 'input' })}>
                  <RadioListItem value="input" label={<>入力：<span className="preserve-lines">{draft[field] || '（空欄）'}</span></>} aria-label={`入力：${draft[field] || '（空欄）'}`} />
                  <RadioListItem value="latest" label={<>最新：<span className="preserve-lines">{latestValues[field] || '（空欄）'}</span></>} aria-label={`最新：${latestValues[field] || '（空欄）'}`} />
                </RadioList>)}
              </VStack></GridSpan>}
              <GridSpan columns="full"><HStack className="shop-editor-actions" justify="end" gap={3}>
                <Button type="button" variant="ghost" size="lg" label="キャンセル" onClick={() => navigate(storeUrl)} />
                <Button type="submit" label="保存" variant="primary" size="lg" isLoading={busy} isDisabled={blocked || !options.data || Boolean(latest && differences.some(field => !choices[field]))} />
              </HStack></GridSpan>
            </Grid>
          </FormLayout>
        </form>
      </Card>
      <div className="shop-editor-danger">
        {deleting
          ? <Banner status="warning" title={`「${shop.name}」を削除しますか？`} description="保存済みの写真・評価・メモも削除します。元に戻せません。元投稿と確認履歴は残ります。" endContent={<HStack gap={2}>
            <Button type="button" variant="ghost" label="キャンセル" isDisabled={busy} onClick={() => setDeleting(false)} />
            <Button type="button" variant="destructive" label="削除する" isLoading={busy} isDisabled={blocked} onClick={() => {
              setBusy(true);
              api(`/api/ui/admin/shops/${shop.id}/delete`, z.object({ ok: z.boolean() }), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version: shop.version }) }).then(() => navigate('/'), (failure: Error) => { setError(failure); setBusy(false); setDeleting(false); });
            }} />
          </HStack>} />
          : <Button type="button" variant="destructive" label="店舗削除" isDisabled={blocked} onClick={() => setDeleting(true)} />}
      </div>
    </main>
  );
}
export function ShopEditor({ id, session }: { id: number; session: Session }): ReactNode {
  const resource = useResource(`/api/ui/shops/${id}`, detailSchema);
  if (!session.is_admin) return <main className="main-shell"><ErrorBox error="管理者権限が必要です。" /></main>;
  if (resource.loading) return <Skeleton />;
  return resource.data ? <Editor key={resource.data.id} shop={resource.data} session={session} /> : <main className="main-shell"><ErrorBox error={resource.error} retry={resource.reload} /></main>;
}
