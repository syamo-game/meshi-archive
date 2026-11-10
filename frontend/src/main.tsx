import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import { InternationalizationProvider } from '@astryxdesign/core/i18n';
import jaJP from '@astryxdesign/core/locales/ja-JP.generated.js';
import { Card } from '@astryxdesign/core/Card';
import { Button } from '@astryxdesign/core/Button';
import { DropdownMenu } from '@astryxdesign/core/DropdownMenu';
import { HStack } from '@astryxdesign/core/HStack';
import { Theme } from '@astryxdesign/core/theme';
import { ToastViewport } from '@astryxdesign/core/Toast';
import { meshiTheme } from './theme';
import { lazy, Suspense, useEffect } from 'react';
import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { sessionSchema, setCsrf } from './api';
import type { Session } from './api';
import { Explore } from './Explore';
import { ShopDialog } from './Shop';
import { ErrorBoundary, ErrorBox, Icon, Link, Skeleton, navigate, useLocation, useResource } from './shared';
import './styles.css';
import './app.css';

const ShopEditor = lazy(() => import('./ShopEditor').then(module => ({ default: module.ShopEditor })));
const AdminRoutes = lazy(() => import('./AdminRoutes'));
const initialError = new URLSearchParams(window.location.search).get('error');
const sessionUrl = '/api/ui/session' + (initialError ? '?error=' + encodeURIComponent(initialError) : '');
function Authentication({ session, logout = false, admin = false }: { session: Session; logout?: boolean; admin?: boolean }): ReactNode {
  return <main className="main-shell auth-page"><Card className="auth-panel" padding={6} maxWidth={400}><h1>{logout ? 'ログアウト' : admin ? '管理者ログイン' : 'ログイン'}</h1><ErrorBox error={session.login_error} />{logout ? <form action="/logout" method="post"><input type="hidden" name="csrf_token" value={session.csrf_token} /><HStack justify="end" gap={3}><Button type="button" variant="ghost" label="キャンセル" onClick={() => navigate('/')} /><Button type="submit" variant="primary" label="ログアウト" /></HStack></form> : session.discord_enabled ? <Button href="/auth/discord" variant="primary" width="100%" label="Discordでログイン" /> : <p role="alert">Discord認証の設定が必要です。管理者にお問い合わせください。</p>}</Card></main>;
}
function Screen({ session, location }: { session: Session; location: string }): ReactNode {
  const url = new URL(location, window.location.origin);
  const path = url.pathname;
  const auth = path === '/login' || path === '/admin/login' || path === '/logout';
  useEffect(() => { if (!session.authenticated && !auth) navigate('/login', true); }, [session.authenticated, auth]);
  if (auth) return <Authentication session={session} admin={path === '/admin/login'} logout={path === '/logout'} />;
  if (!session.authenticated) return <Skeleton />;
  if (path.startsWith('/admin')) {
    if (!session.is_admin) return <main className="main-shell"><ErrorBox error="管理者権限が必要です。" /></main>;
    return <Suspense fallback={<Skeleton />}><AdminRoutes session={session} path={path} location={location} /></Suspense>;
  }
  const match = /^\/shop\/(\d+)$/.exec(path);
  if (match?.[1]) {
    const id = Number(match[1]);
    if (url.searchParams.get('edit') === 'true') return <Suspense fallback={<Skeleton />}><ShopEditor id={id} session={session} /></Suspense>;
    return <ShopDialog id={id} session={session} saved={url.searchParams.get('saved') === 'true'} close={() => { const target = url.searchParams.get('return_to'); navigate(target?.startsWith('/?') || target === '/' ? target : '/'); }} />;
  }
  if (path === '/') return <Explore location={location} session={session} />;
  return <main className="main-shell"><h1 className="sr-only">ページが見つかりません</h1><ErrorBox error="ページが見つかりません。" /><Link href="/">店舗一覧</Link></main>;
}
function App(): ReactNode {
  const location = useLocation();
  const resource = useResource(sessionUrl, sessionSchema);
  useEffect(() => { if (resource.data) setCsrf(resource.data.csrf_token); }, [resource.data]);
  if (!resource.data && resource.loading) return <Skeleton />;
  if (!resource.data) return <main className="main-shell"><ErrorBox error={resource.error} retry={resource.reload} /></main>;
  const session = resource.data;
  return <div className="has-entered"><header className="site-header"><div className="header-inner"><Link href="/" className="brand" label="Meshi Archive" color="primary"><img className="brand-mark" src="/static/apple-icon.png" alt="" /><span><strong>meshi</strong> archive</span></Link><div className="account-menu"><DropdownMenu button={{ label: 'メニュー', icon: <Icon name="menu" />, isIconOnly: true, variant: 'ghost' }} alignment="end" items={[...(session.is_admin ? [{ label: '管理', onClick: () => navigate('/admin/') }] : []), { label: session.authenticated ? 'ログアウト' : 'ログイン', onClick: () => navigate(session.authenticated ? '/logout' : '/login') }]} /></div></div></header><Screen session={session} location={location} /></div>;
}
const root = document.getElementById('root');
if (!root) throw new Error('React root element is missing');
createRoot(root).render(<InternationalizationProvider locale="ja-JP" messages={{ 'ja-JP': jaJP }}><Theme theme={meshiTheme} mode="light"><ToastViewport position="bottomEnd"><ErrorBoundary><App /></ErrorBoundary></ToastViewport></Theme></InternationalizationProvider>);
