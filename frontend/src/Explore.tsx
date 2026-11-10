import { blockComboboxSubmit } from './ui';
import { Button } from '@astryxdesign/core/Button';
import { TextInput } from '@astryxdesign/core/TextInput';
import { Selector } from '@astryxdesign/core/Selector';
import { Pagination } from '@astryxdesign/core/Pagination';
import { Badge } from '@astryxdesign/core/Badge';
import { ToggleButton, ToggleButtonGroup } from '@astryxdesign/core/ToggleButton';
import { ClickableCard } from '@astryxdesign/core/ClickableCard';
import { Field } from '@astryxdesign/core/Field';
import { SegmentedControl, SegmentedControlItem } from '@astryxdesign/core/SegmentedControl';
import { Token } from '@astryxdesign/core/Token';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { HStack } from '@astryxdesign/core/HStack';
import { useEffect, useId, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { shopsSchema } from './api';
import type { Session, Shop, ShopPage } from './api';
import { ErrorBox, ExternalButton, Icon, Modal, Skeleton, Stars, closeDialog, navigate, openDialog, useResource } from './shared';
import { ShopDialog } from './Shop';
import { CategorySelect } from './CategorySelect';

const sortOptions = [
  { value: 'created_at_desc', label: '追加順' }, { value: 'rating_desc', label: '評価順' }, { value: 'rating_asc', label: '評価順（昇順）' },
  { value: 'name_asc', label: '店名順' }, { value: 'name_desc', label: '店名順（逆順）' }, { value: 'area_order', label: '地域順' },
  { value: 'area_asc', label: 'エリア名順' }, { value: 'area_desc', label: 'エリア名順（逆順）' },
];
function Photo({ shop }: { shop: Shop }): ReactNode {
  const [failed, setFailed] = useState(false);
  if (!shop.image_url || failed) return <span className="photo-empty">{failed ? '写真を読み込めませんでした' : '写真なし'}</span>;
  return <img className="card-image" src={shop.image_url} alt={`${shop.name}の写真`} loading="lazy" width="500" height="320" onError={() => { console.error('Shop photo failed', { shopId: shop.id }); setFailed(true); }} />;
}
function Filter({ page, params, close, apply }: { page: ShopPage; params: URLSearchParams; close: () => void; apply: (values: FormData) => void }): ReactNode {
  const [category, setCategory] = useState(params.get('category') ?? '');
  const [area, setArea] = useState(params.get('area') ?? '');
  const [status, setStatus] = useState(params.get('status') ?? 'all');
  const statusId = useId();
  const areaOptions = [{ value: '', label: 'すべて' }, ...page.filter_areas.map(entry => ({ value: entry.value, label: `${'　'.repeat(entry.depth)}${entry.label} (${entry.count})` })), { value: '__none__', label: 'エリア未設定' }];
  return <Modal title="絞り込み" close={close} footer={<HStack justify="between" width="100%"><Button type="button" variant="ghost" label="リセット" onClick={() => { const data = new FormData(); data.set('area', ''); data.set('category', ''); data.set('status', 'all'); apply(data); }} /><Button type="submit" form="explore-filter" variant="primary" label="適用" /></HStack>}><form id="explore-filter" className="filter-body" onKeyDown={blockComboboxSubmit} onSubmit={event => { event.preventDefault(); apply(new FormData(event.currentTarget)); }}><Selector label="エリア" htmlName="area" options={areaOptions} value={area} onChange={setArea} hasSearch searchPlaceholder="エリアを検索" width="100%" /><CategorySelect label="カテゴリ" name="category" value={category} onChange={setCategory} groups={page.category_groups} emptyLabel="すべて" includeUnclassified /><Field label="訪問状況" inputID={statusId} isGroupLabel width="100%"><SegmentedControl id={statusId} label="訪問状況" layout="fill" value={status} onChange={setStatus}><SegmentedControlItem value="all" label="すべて" /><SegmentedControlItem value="unvisited" label="未訪問" /><SegmentedControlItem value="visited" label="訪問済み" /></SegmentedControl></Field><input type="hidden" name="status" value={status} /></form></Modal>;
}
export function Explore({ location, session }: { location: string; session: Session }): ReactNode {
  const params = new URL(location, window.location.origin).searchParams;
  const listParams = new URLSearchParams(params); listParams.delete('detail');
  const key = listParams.toString();
  const resource = useResource('/api/ui/shops?' + key, shopsSchema);
  const [query, setQuery] = useState(params.get('q') ?? '');
  const [filters, setFilters] = useState(false);
  useEffect(() => { setQuery(new URLSearchParams(key).get('q') ?? ''); window.scrollTo({ top: 0, behavior: 'instant' }); }, [key]);
  const change = (values: Record<string, string>, reset = true): void => {
    const next = new URLSearchParams(key);
    for (const [name, value] of Object.entries(values)) { if (value && value !== 'all') next.set(name, value); else next.delete(name); }
    if (reset) next.delete('page');
    navigate('/' + (next.size ? '?' + next.toString() : ''));
  };
  const submit = (event: FormEvent<HTMLFormElement>): void => { event.preventDefault(); change({ q: query }); };
  const data = resource.data;
  const detail = params.get('detail');
  if (!data && resource.loading) return <Skeleton />;
  if (!data) return <main className="main-shell"><ErrorBox error={resource.error} retry={resource.reload} /></main>;
  const active: { name: string; label: string }[] = [];
  for (const name of ['q', 'area', 'category', 'status']) {
    const value = params.get(name);
    if (value && value !== 'all') active.push({ name, label: name === 'status' ? (value === 'visited' ? '訪問済み' : '未訪問') : name === 'area' ? [...data.filter_areas, ...data.areas].find(area => area.value === value)?.label ?? value : name === 'category' && value === '__none__' ? '未分類' : value });
  }
  const filterCount = active.filter(item => item.name !== 'q').length;
  // Area chips follow the current filters; keep the selected area visible even when it drops out of the top list.
  const selectedArea = params.get('area') ?? '';
  const topAreas = data.areas.slice(0, 8);
  const areaChips = selectedArea && !topAreas.some(area => area.value === selectedArea) ? [{ value: selectedArea, label: selectedArea === '__none__' ? 'エリア未設定' : data.filter_areas.find(area => area.value === selectedArea)?.label ?? selectedArea, count: data.total }, ...topAreas.slice(0, 7)] : topAreas;
  return <><main className="main-shell" inert={resource.loading && !detail}><section className="search-section">
    <form className="astryx-search" onSubmit={submit}>
      <TextInput id="query" type="search" enterKeyHint="search" label="検索" isLabelHidden size="lg" containerClassName="astryx-search-field" controlClassName="astryx-search-control" placeholder="店名・エリア・カテゴリ" value={query} onChange={setQuery} />
      <Button className="astryx-search-submit" type="submit" size="lg" variant="primary" label="検索" icon={<Icon name="search" />}><span className="astryx-search-label">検索</span></Button>
      <div className="astryx-filter-control">
        <Button className="astryx-filter-button" type="button" size="lg" variant="secondary" label="絞り込み" aria-haspopup="dialog" icon={<Icon name="filter" />} onClick={() => setFilters(true)}><span className="astryx-search-label">絞り込み</span></Button>
        {filterCount > 0 && <Badge className="filter-count" variant="info" label={filterCount} />}
      </div>
    </form>
  </section><div className="quick-row"><div className="area-tabs"><ToggleButtonGroup label="エリアで絞り込む" size="sm" value={selectedArea || null} onChange={area => change({ area: area ?? '' })}>{areaChips.map(area => <ToggleButton key={area.value} value={area.value} label={`${area.label} ${area.count}件`}>{area.label}<span className="area-count">{area.count}</span></ToggleButton>)}</ToggleButtonGroup></div></div><section className="results" aria-label="店舗一覧" aria-busy={resource.loading}><div className="results-header"><h1 className="results-title">店舗一覧 <span id="result-count">{data.total}</span></h1><div className="list-sort"><Selector label="並び順" isLabelHidden size="sm" options={sortOptions} value={params.get('sort') ?? 'created_at_desc'} onChange={sort => change({ sort })} /></div></div>{active.length > 0 && <div className="active-filters">{active.map(item => <Token key={item.name} label={item.label} color="blue" onRemove={() => change({ [item.name]: '' })} />)}{data.total > 0 && <Button variant="ghost" size="sm" label="全解除" onClick={() => navigate('/')} />}</div>}<ErrorBox error={resource.error} retry={resource.reload} /><div className="cards">{data.items.map(shop => <ClickableCard className="place-card" padding={0} elevation="low" key={shop.id} label={`${shop.name}の詳細を開く`} onClick={() => openDialog('/?' + new URLSearchParams({ ...Object.fromEntries(listParams), detail: String(shop.id) }).toString())}><div className="photo-link"><Photo shop={shop} /></div><div className="card-body"><h2 className="card-heading"><span className="title-button">{shop.name}{shop.branch ? ` ${shop.branch}` : ''}</span></h2><p className="card-meta"><span className="card-location">{shop.area_label}</span>{shop.area_label && shop.category && <span aria-hidden="true">/</span>}<span className="card-genre">{shop.category}</span></p><p className="card-memo">{shop.memo}</p></div><footer className="card-footer"><Stars rating={shop.rating} /><div className="card-actions"><ExternalButton href={shop.maps_url} className="map-link" label={`${shop.name}のMap`} icon={<Icon name="pin" />}>Map</ExternalButton><ExternalButton href={shop.source_url} className="source-link" label={`${shop.name}の${shop.source_label ?? '元投稿'}`} icon={<Icon name="source" />}>{shop.source_label === 'X' ? 'X' : <span className="source-text">{shop.source_label}</span>}</ExternalButton></div></footer></ClickableCard>)}</div>{data.total === 0 && <EmptyState title="店舗が見つかりません" headingLevel={2} actions={<Button variant="primary" label="全解除" onClick={() => navigate('/')} />} />}{data.pages > 1 && <Pagination className="pagination" label="ページ" page={data.page} totalPages={data.pages} siblingCount={1} onChange={page => change({ page: String(page) }, false)} />}</section></main>{filters && <Filter page={data} params={params} close={() => setFilters(false)} apply={values => { change({ area: String(values.get('area') ?? ''), category: String(values.get('category') ?? ''), status: String(values.get('status') ?? 'all') }); setFilters(false); }} />}{detail && /^\d+$/.test(detail) && <ShopDialog id={Number(detail)} session={session} close={() => { const next = new URLSearchParams(key); closeDialog('/' + (next.size ? '?' + next.toString() : '')); }} changed={resource.reload} />}</>;
}
