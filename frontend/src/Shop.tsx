import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { api, detailSchema } from './api';
import type { Session, ShopDetail } from './api';
import { ErrorBox, ExternalLink, Icon, Link, Modal, Skeleton, useResource } from './shared';

export function ShopDialog({ id, session, close, changed }: { id: number; session: Session; close: () => void; changed?: () => void }): ReactNode {
  const resource = useResource(`/api/ui/shops/${id}`, detailSchema);
  const [shop, setShop] = useState<ShopDetail | null>(null);
  const [editing, setEditing] = useState(false);
  const [memo, setMemo] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => { setShop(resource.data); }, [resource.data]);
  const update = async (annotation: { rating: number | null } | { memo: string }): Promise<void> => {
    if (!shop) return;
    setBusy(true); setError(null);
    try {
      const next = await api(`/api/ui/shops/${shop.id}/annotation`, detailSchema, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...annotation, version: shop.version }) });
      setShop(next); setEditing(false); changed?.();
    } catch (failure) { if (failure instanceof Error) setError(failure); else { console.error('Annotation failed', failure); setError(new Error('保存できませんでした。')); } }
    finally { setBusy(false); }
  };
  return <Modal title="店舗詳細" close={close} detail><header className="sheet-header detail-top"><span>店舗詳細</span><button className="icon-button" aria-label="店舗詳細を閉じる" onClick={close}><Icon name="close" /></button></header><div className="detail-content">{resource.loading && !shop && <Skeleton full={false} />}<ErrorBox error={resource.error} retry={resource.reload} />{shop && <>{shop.image_url && <img className="detail-image" src={shop.image_url} alt={`${shop.name}の写真`} onError={() => { console.error('Shop detail photo failed', { shopId: shop.id }); setError(new Error('写真を読み込めませんでした。')); }} />}<div className="detail-info"><h2>{shop.name}{shop.branch ? ` ${shop.branch}` : ''}</h2><p className="detail-meta"><span>{shop.area_label}</span>{shop.area_label && shop.category && <span aria-hidden="true">/</span>}<span>{shop.category}</span></p><fieldset className="rating-editor" disabled={busy || !session.can_annotate}><legend>評価</legend><div className="rating-editor__row"><div className="rating-options">{Array.from({ length: 5 }, (_, index) => <label className={`rating-choice ${index < (shop.rating ?? 0) ? 'is-selected' : ''}`} key={index}><input className="sr-only" type="radio" name="rating" value={index + 1} checked={shop.rating === index + 1} aria-label={`${index + 1}点`} onChange={() => void update({ rating: index + 1 })} /><span aria-hidden="true">★</span></label>)}</div><button className="text-button rating-reset" type="button" disabled={!shop.rating} onClick={() => void update({ rating: null })}>解除</button></div></fieldset><section className="memo-section" aria-labelledby="memo-heading"><div className="memo-heading"><h3 id="memo-heading">メモ</h3>{session.can_annotate && !editing && <button className="text-button" aria-label="メモを編集" onClick={() => { setMemo(shop.memo); setEditing(true); }}>編集</button>}</div>{editing ? <form onSubmit={event => { event.preventDefault(); void update({ memo }); }}><label className="sr-only" htmlFor="memo-input">メモ</label><textarea id="memo-input" className="memo-input" rows={4} maxLength={20000} value={memo} onChange={event => setMemo(event.target.value)} disabled={busy} autoFocus /><div className="memo-actions"><button type="button" className="text-button" disabled={busy} onClick={() => setEditing(false)}>キャンセル</button><button className="primary-button" disabled={busy}>保存</button></div></form> : shop.memo && <p className="memo-content">{shop.memo}</p>}</section><ErrorBox error={error} />{error && <button className="text-button" disabled={busy} onClick={resource.reload}>最新情報を確認</button>}<ExternalLink href={shop.source_url} className="source-link detail-source"><Icon name="source" />{shop.source_label ?? '元投稿'}</ExternalLink>{shop.canonical_url && shop.canonical_url !== shop.source_url && <ExternalLink href={shop.canonical_url} className="text-button detail-reference">店舗ページ</ExternalLink>}{shop.address && <p className="detail-fact">{shop.address}</p>}{shop.phone && <p className="detail-fact">{shop.phone}</p>}{session.is_admin && <div className="detail-admin"><Link className="text-button" href={`/shop/${shop.id}?edit=true`}>店舗編集</Link><ExternalLink href={shop.discord_url} className="text-button">Discord</ExternalLink></div>}</div></>}</div><footer className="detail-footer"><ExternalLink href={shop?.maps_url ?? null} className="primary-button"><Icon name="pin" />Map</ExternalLink></footer></Modal>;
}
