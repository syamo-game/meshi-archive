import { TextArea } from '@astryxdesign/core/TextArea';
import { Banner } from '@astryxdesign/core/Banner';
import { Link as AstryxLink } from '@astryxdesign/core/Link';
import { useToast } from '@astryxdesign/core/Toast';
import { Button } from '@astryxdesign/core/Button';
import { HStack } from '@astryxdesign/core/HStack';
import { MetadataList, MetadataListItem } from '@astryxdesign/core/MetadataList';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, detailSchema } from './api';
import type { Session, ShopDetail } from './api';
import { ErrorBox, ExternalButton, ExternalLink, Icon, Link, Modal, RatingInput, Skeleton, useResource } from './shared';

export function ShopDialog({ id, session, close, changed, saved = false }: { id: number; session: Session; close: () => void; changed?: () => void; saved?: boolean }): ReactNode {
  const resource = useResource(`/api/ui/shops/${id}`, detailSchema);
  const [shop, setShop] = useState<ShopDetail | null>(null);
  const [editing, setEditing] = useState(false);
  const [memo, setMemo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => { setShop(resource.data); }, [resource.data]);
  // Undo runs later, so it must read the newest version, not the one from an old render.
  const latest = useRef<ShopDetail | null>(null);
  useEffect(() => { latest.current = shop; }, [shop]);
  const toast = useToast();
  const update = async (annotation: { rating: number | null } | { memo: string }): Promise<boolean> => {
    const current = latest.current;
    if (!current) return false;
    setBusy(true); setError(null);
    try {
      const next = await api(`/api/ui/shops/${current.id}/annotation`, detailSchema, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...annotation, version: current.version }) });
      latest.current = next; setShop(next); setEditing(false); changed?.(); return true;
    } catch (failure) { if (failure instanceof Error) setError(failure); else { console.error('Annotation failed', failure); setError(new Error('保存できませんでした。')); } return false; }
    finally { setBusy(false); }
  };
  const changeRating = (rating: number | null): void => {
    const previous = latest.current?.rating ?? null;
    void update({ rating }).then(ok => {
      if (!ok) return;
      const dismiss = toast({ body: rating ? `評価を${rating}点にしました` : '評価を解除しました', uniqueID: 'rating-saved', autoHideDuration: 8000, endContent: <Button variant="ghost" size="sm" label="元に戻す" onClick={() => { dismiss(); void update({ rating: previous }); }} /> });
    });
  };
  return <Modal title="店舗詳細" close={close} detail titleHidden footer={<ExternalButton href={shop?.maps_url ?? null} variant="primary" size="lg" width="100%" label="Map" icon={<Icon name="pin" />} />}>{resource.loading && !shop && <Skeleton full={false} />}<ErrorBox error={resource.error} retry={resource.reload} />{shop && <>{shop.image_url && <img className="detail-image" src={shop.image_url} alt={`${shop.name}の写真`} onError={() => { console.error('Shop detail photo failed', { shopId: shop.id }); setError(new Error('写真を読み込めませんでした。')); }} />}<div className="detail-info">{saved && <Banner className="detail-saved" status="success" title="保存しました。" />}<h2>{shop.name}{shop.branch ? ` ${shop.branch}` : ''}</h2><p className="detail-meta"><span>{shop.area_label}</span>{shop.area_label && shop.category && <span aria-hidden="true">/</span>}<span>{shop.category}</span></p><RatingInput rating={shop.rating} disabled={busy || !session.can_annotate} onChange={changeRating} /><section className="memo-section" aria-labelledby="memo-heading"><div className="memo-heading"><h3 id="memo-heading">メモ</h3>{session.can_annotate && !editing && <Button variant="ghost" size="sm" label="メモを編集" onClick={() => { setMemo(shop.memo); setEditing(true); }}>編集</Button>}</div>{editing ? <form onSubmit={event => { event.preventDefault(); void update({ memo }); }}><TextArea label="メモ" isLabelHidden rows={4} maxLength={20000} value={memo} onChange={setMemo} isDisabled={busy} hasAutoFocus /><HStack className="memo-actions" justify="end" gap={3}><Button type="button" variant="ghost" label="キャンセル" isDisabled={busy} onClick={() => setEditing(false)} /><Button type="submit" variant="primary" label="保存" isLoading={busy} /></HStack></form> : shop.memo && <p className="memo-content">{shop.memo}</p>}</section><ErrorBox error={error} />{error && <Button variant="ghost" size="sm" label="最新情報を確認" isDisabled={busy} onClick={resource.reload} />}{(shop.source_url || (shop.canonical_url && shop.canonical_url !== shop.source_url)) && <HStack className="detail-links" align="center" gap={4} wrap="wrap"><ExternalButton href={shop.source_url} className="detail-source" label={shop.source_label ?? '元投稿'} icon={<Icon name="source" />} />{shop.canonical_url && shop.canonical_url !== shop.source_url && <ExternalLink href={shop.canonical_url} className="detail-reference">店舗ページ</ExternalLink>}</HStack>}{(shop.address || shop.phone) && <MetadataList className="detail-facts">{shop.address && <MetadataListItem label="住所">{shop.address}</MetadataListItem>}{shop.phone && <MetadataListItem label="電話番号">{/^[0-9+() -]+$/.test(shop.phone) ? <AstryxLink href={`tel:${shop.phone.replace(/[^0-9+]/g, '')}`}>{shop.phone}</AstryxLink> : shop.phone}</MetadataListItem>}</MetadataList>}{session.is_admin && <div className="detail-admin"><Link href={`/shop/${shop.id}?edit=true`}>店舗編集</Link><ExternalLink href={shop.discord_url}>Discord</ExternalLink></div>}</div></>}</Modal>;
}
