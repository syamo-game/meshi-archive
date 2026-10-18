import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { sessionSchema, setCsrf } from './api';
import type { Session } from './api';
import { AdminNav, Bulk, Exports, Overview, Users } from './Admin';
import { Explore } from './Explore';
import { Reviews } from './Review';
import { ShopDialog } from './Shop';
import { ShopEditor } from './ShopEditor';
import { ErrorBoundary, ErrorBox, Icon, Link, Skeleton, navigate, useLocation, useResource } from './shared';
import './styles.css';
import './app.css';

const initialError = new URLSearchParams(window.location.search).get('error');
const sessionUrl = '/api/ui/session' + (initialError ? '?error=' + encodeURIComponent(initialError) : '');
function Authentication({ session, logout = false, admin = false }: { session: Session; logout?: boolean; admin?: boolean }): ReactNode {
  return <main className="main-shell auth-page"><section className="auth-panel"><h1>{logout ? 'ログアウト' : admin ? '管理者ログイン' : 'ログイン'}</h1><ErrorBox error={session.login_error} />{logout ? <form action="/logout" method="post"><input type="hidden" name="csrf_token" value={session.csrf_token} /><div className="form-actions"><Link href="/" className="text-button">キャンセル</Link><button className="primary-button">ログアウト</button></div></form> : session.discord_enabled ? <a href="/auth/discord" className="primary-button">Discordでログイン</a> : <p role="alert">Discord認証の設定が必要です。管理者にお問い合わせください。</p>}</section></main>;
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
    let page: ReactNode;
    if (path === '/admin/users') page = <Users session={session} />;
    else if (path === '/admin/exports') page = <Exports />;
    else if (path === '/admin/shops/bulk') page = <Bulk session={session} />;
    else if (path === '/admin/review') page = <Reviews session={session} location={location} />;
    else page = <Overview session={session} />;
    return <><AdminNav />{session.read_only && <p className="read-only-banner" role="status">読取専用</p>}{page}</>;
  }
  const match = /^\/shop\/(\d+)$/.exec(path);
  if (match?.[1]) {
    const id = Number(match[1]);
    if (url.searchParams.get('edit') === 'true') return <ShopEditor id={id} session={session} />;
    return <ShopDialog id={id} session={session} close={() => { const target = url.searchParams.get('return_to'); navigate(target?.startsWith('/?') || target === '/' ? target : '/'); }} />;
  }
  if (path === '/') return <Explore location={location} session={session} />;
  return <main className="main-shell"><ErrorBox error="ページが見つかりません。" /><Link href="/">店舗一覧</Link></main>;
}
function App(): ReactNode {
  const location = useLocation();
  const resource = useResource(sessionUrl, sessionSchema);
  useEffect(() => { if (resource.data) setCsrf(resource.data.csrf_token); }, [resource.data]);
  if (!resource.data && resource.loading) return <Skeleton />;
  if (!resource.data) return <main className="main-shell"><ErrorBox error={resource.error} retry={resource.reload} /></main>;
  const session = resource.data;
  return <div className="has-entered"><header className="site-header"><div className="header-inner"><Link href="/" className="brand" label="Meshi Archive"><img className="brand-mark" src="/static/apple-icon.png" alt="" /><span><strong>meshi</strong> archive</span></Link><details className="account-menu" key={location}><summary aria-label="メニュー"><Icon name="menu" /></summary><nav aria-label="アカウント">{session.is_admin && <Link href="/admin/">管理</Link>}<Link href={session.authenticated ? '/logout' : '/login'}>{session.authenticated ? 'ログアウト' : 'ログイン'}</Link></nav></details></div></header><Screen session={session} location={location} /></div>;
}
const root = document.getElementById('root');
if (!root) throw new Error('React root element is missing');
createRoot(root).render(<ErrorBoundary><App /></ErrorBoundary>);
