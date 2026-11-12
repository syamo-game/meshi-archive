import { Dialog } from '@astryxdesign/core/Dialog';
import { DialogHeader } from '@astryxdesign/core/Dialog';
import { Layout, LayoutContent, LayoutFooter } from '@astryxdesign/core/Layout';
import { Button } from '@astryxdesign/core/Button';
import { HStack } from '@astryxdesign/core/HStack';
import { Skeleton as AstryxSkeleton } from '@astryxdesign/core/Skeleton';
import { Banner } from '@astryxdesign/core/Banner';
import { Link as AstryxLink } from '@astryxdesign/core/Link';
import { Component, useEffect, useState } from 'react';
import type { ComponentProps, ErrorInfo, ReactNode } from 'react';
import { z } from 'zod';
import { ApiError, api } from './api';

export function navigate(url: string, replace = false): void {
  if (!replace && url === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState(null, '', url); else window.history.pushState(null, '', url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}
export function openDialog(url: string): void {
  window.history.pushState({ dialogReturn: window.location.pathname + window.location.search }, '', url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}
export function closeDialog(fallback: string): void {
  const state = z.object({ dialogReturn: z.string() }).safeParse(window.history.state);
  if (state.success && state.data.dialogReturn === fallback) window.history.back();
  else navigate(fallback, true);
}
// Swap the open dialog for another one without adding a history entry.
export function replaceDialog(url: string): void {
  window.history.replaceState(window.history.state, '', url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}
export function useLocation(): string {
  const [location, setLocation] = useState(window.location.pathname + window.location.search);
  useEffect(() => { const change = (): void => setLocation(window.location.pathname + window.location.search); window.addEventListener('popstate', change); return () => window.removeEventListener('popstate', change); }, []);
  return location;
}
export function Link({ href, children, className, label, current, color }: { href: string; children: ReactNode; className?: string; label?: string; current?: 'page'; color?: ComponentProps<typeof AstryxLink>['color'] }): ReactNode {
  return <AstryxLink href={href} className={className} label={label} aria-current={current} color={color} onClick={event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); navigate(href); } }}>{children}</AstryxLink>;
}
export function ExternalLink({ href, children, className, label }: { href: string | null; children: ReactNode; className?: string; label?: string }): ReactNode {
  if (!href) return null;
  const valid = /^https?:\/\//i.test(href) || (href.startsWith('/') && !href.startsWith('//'));
  return valid ? <AstryxLink href={href} target="_blank" rel="noopener noreferrer" className={className} label={label}>{children}</AstryxLink> : <span className="field-error">リンクの形式を確認してください。</span>;
}
// Button-styled external link; keeps the same URL safety check as ExternalLink.
export function ExternalButton({ href, children, className, label, icon, variant = 'secondary', size = 'sm', width }: { href: string | null; children?: ReactNode; className?: string; label: string; icon?: ReactNode; variant?: 'primary' | 'secondary' | 'ghost'; size?: 'sm' | 'md' | 'lg'; width?: string }): ReactNode {
  if (!href) return null;
  const valid = /^https?:\/\//i.test(href) || (href.startsWith('/') && !href.startsWith('//'));
  return valid ? <Button href={href} target="_blank" rel="noopener noreferrer" className={className} label={label} icon={icon} variant={variant} size={size} width={width}>{children}</Button> : <span className="field-error">リンクの形式を確認してください。</span>;
}
export function useResource<T>(url: string, schema: z.ZodType<T>): { data: T | null; error: Error | null; loading: boolean; reload: () => void } {
  const [result, setResult] = useState<{ data: T | null; error: Error | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setResult(previous => ({ data: previous.data, error: null, loading: true }));
    api(url, schema, { signal: controller.signal }).then(data => setResult({ data, error: null, loading: false }), (error: Error) => { if (error.name !== 'AbortError') setResult(previous => ({ data: previous.data, error, loading: false })); });
    return () => controller.abort();
  }, [url, schema, revision]);
  return { ...result, reload: () => setRevision(value => value + 1) };
}
export type IconName = 'search' | 'filter' | 'close' | 'pin' | 'source' | 'next' | 'previous' | 'menu' | 'edit' | 'plus';
export function Icon({ name }: { name: IconName }): ReactNode {
  const paths: Record<IconName, ReactNode> = {
    search: <><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4.5 4.5" /></>,
    filter: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="17" r="2" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    pin: <><path d="M12 21s7-5.3 7-12a7 7 0 1 0-14 0c0 6.7 7 12 7 12Z" /><circle cx="12" cy="9" r="2.5" /></>,
    source: <><path d="m10 8 3-3a4.24 4.24 0 0 1 6 6l-3 3M14 16l-3 3a4.24 4.24 0 0 1-6-6l3-3m0 8 8-8" /></>,
    next: <path d="m9 5 7 7-7 7" />, previous: <path d="m15 5-7 7 7 7" />,
    menu: <path d="M4 6h16M4 12h16M4 18h16" />, edit: <><path d="m15 4 5 5-10 10H5v-5L15 4Z" /></>, plus: <path d="M12 5v14M5 12h14" />,
  };
  return <svg className="meshi-icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}
export function ErrorBox({ error, retry }: { error: Error | string | null; retry?: () => void }): ReactNode {
  if (!error) return null;
  return <Banner className="error-box" status="error" title={<span className="preserve-lines">{typeof error === 'string' ? error : error.message}</span>} endContent={error instanceof ApiError && error.status === 401 ? <AstryxLink href="/login">ログイン</AstryxLink> : retry && <Button variant="ghost" size="sm" label="再試行" onClick={retry} />} />;
}
export function Skeleton({ full = true }: { full?: boolean }): ReactNode {
  return <div className={full ? 'react-skeleton fullscreen' : 'react-skeleton'} role="status" aria-label="読み込み中"><div className="skeleton-inner"><div className="skeleton-header"><AstryxSkeleton className="sk" style={{ width: 160, height: 28 }} /></div><AstryxSkeleton className="sk sk-search" /><div className="skeleton-heading"><AstryxSkeleton className="sk" /></div><div className="skeleton-cards">{Array.from({ length: 6 }, (_, i) => <div className="skeleton-card" key={i}><AstryxSkeleton className="sk sk-photo" /><div className="sk-copy"><AstryxSkeleton className="sk" /><AstryxSkeleton className="sk" /><AstryxSkeleton className="sk" /></div></div>)}</div></div></div>;
}
// titleHidden: no title bar; the title still names the dialog and a close button floats over the content.
// unsaved: closing asks in place first, so Escape or a backdrop click cannot drop the input.
export function Modal({ children, title, close, detail = false, titleHidden = false, unsaved = false, footer }: { children: ReactNode; title: string; close: () => void; detail?: boolean; titleHidden?: boolean; unsaved?: boolean; footer?: ReactNode }): ReactNode {
  const [asking, setAsking] = useState(false);
  useEffect(() => { if (!unsaved) setAsking(false); }, [unsaved]);
  const requestClose = (): void => { if (unsaved) setAsking(true); else close(); };
  const onOpenChange = (open: boolean): void => { if (!open) requestClose(); };
  return <Dialog isOpen onOpenChange={onOpenChange} padding={0} maxHeight="var(--sheet-max-height)" className={`sheet ${detail ? 'detail-sheet' : 'filter-sheet'}${titleHidden ? ' sheet-untitled' : ''}`} aria-label={title}>{titleHidden && <Button className="sheet-close" variant="secondary" label="閉じる" icon={<Icon name="close" />} isIconOnly onClick={requestClose} />}<Layout className="sheet-layout" padding={4} header={titleHidden ? undefined : <DialogHeader title={title} onOpenChange={onOpenChange} />} content={<LayoutContent className="sheet-body" isScrollable padding={0}>{children}{asking && <div className="sheet-unsaved"><Banner status="warning" title="保存していない入力があります" endContent={<HStack gap={2}><Button variant="ghost" size="sm" label="編集に戻る" onClick={() => setAsking(false)} /><Button variant="destructive" size="sm" label="破棄して閉じる" onClick={close} /></HStack>} /></div>}</LayoutContent>} footer={footer && <LayoutFooter>{footer}</LayoutFooter>} /></Dialog>;
}
export function Stars({ rating }: { rating: number | null }): ReactNode {
  return <span className="rating" aria-label={rating ? `評価 ${rating}点（5点満点）` : '未評価'}>{rating ? <span className="stars" aria-hidden="true">{Array.from({ length: 5 }, (_, index) => <span key={index} className={index < rating ? 'is-selected' : ''}>★</span>)}</span> : '未評価'}</span>;
}
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error): { error: Error } { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo): void { console.error('React screen failed', error, info.componentStack); }
  render(): ReactNode { return this.state.error ? <main className="main-shell"><ErrorBox error="画面を表示できませんでした。再読み込みしてください。" retry={() => window.location.reload()} /></main> : this.props.children; }
}

export function RatingInput({ rating, onChange, disabled = false, name = 'rating' }: { rating: number | null; onChange: (rating: number | null) => void; disabled?: boolean; name?: string }): ReactNode {
  return <fieldset className="rating-editor" disabled={disabled}><legend>評価</legend><div className="rating-editor__row"><div className="rating-options">{Array.from({ length: 5 }, (_, index) => <label className={`rating-choice ${index < (rating ?? 0) ? 'is-selected' : ''}`} key={index}><input className="sr-only" type="radio" name={name} value={index + 1} checked={rating === index + 1} aria-label={`${index + 1}点`} onChange={() => onChange(index + 1)} /><span aria-hidden="true">★</span></label>)}</div><Button className="rating-reset" type="button" variant="ghost" size="sm" label="解除" isDisabled={!rating} onClick={() => onChange(null)} /></div></fieldset>;
}
