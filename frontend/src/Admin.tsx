import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, csrf, failuresSchema, feedbackSchema, jsonBody, reviewSchema, reviewsSchema, usersSchema } from './api';
import type { Feedback, Session, User } from './api';
import { ErrorBox, ExternalLink, Link, Skeleton, navigate, useResource } from './shared';

export function AdminNav(): ReactNode {
  return <nav className="admin-nav" aria-label="管理"><Link href="/admin/">概要</Link><Link href="/admin/review">データ確認</Link><Link href="/admin/shops/bulk">一括編集</Link><Link href="/admin/exports">データ出力</Link><Link href="/admin/users">利用ユーザー</Link></nav>;
}
function Failures({ session }: { session: Session }): ReactNode {
  const [cursor, setCursor] = useState('');
  const resource = useResource('/api/admin/review-failures?active_only=true' + (cursor ? '&cursor=' + cursor : ''), failuresSchema);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  return <section className="admin-section"><h2>登録できなかった投稿</h2><ErrorBox error={resource.error ?? error} retry={resource.reload} />{resource.loading && <p role="status">読み込み中…</p>}{resource.data?.items.map(item => <article className="admin-row" key={item.message_id}><div><p className="preserve-lines">{item.message_content}</p><p className="field-error">{item.error}</p><ExternalLink href={item.discord_url} className="text-button">Discord</ExternalLink></div>{item.review_mention_id ? <Link className="primary-button" href={`/admin/review?mention=${item.review_mention_id}`}>確認</Link> : <button className="primary-button" disabled={busy || session.read_only} onClick={() => { setBusy(true); setError(null); api(`/api/admin/review-failures/${item.message_id}/prepare`, reviewSchema, jsonBody({})).then(result => navigate(`/admin/review?mention=${result.id}`), (failure: Error) => { setError(failure); setBusy(false); }); }}>確認項目を作成</button>}</article>)}{resource.data?.items.length === 0 && <p>対象の投稿はありません。</p>}{resource.data?.next_cursor && <button className="text-button" onClick={() => setCursor(resource.data?.next_cursor ?? '')}>次へ</button>}</section>;
}
export function Overview({ session }: { session: Session }): ReactNode {
  const resource = useResource('/api/admin/reviews?status=unresolved&limit=1', reviewsSchema);
  return <main className="main-shell admin-page"><h1>概要</h1><ErrorBox error={resource.error} retry={resource.reload} /><section className="admin-summary"><div><h2>未確認</h2><strong>{resource.data?.counters.unresolved ?? '—'}</strong></div><Link className="primary-button" href="/admin/review">データ確認</Link></section><Failures session={session} /></main>;
}
function UserEditor({ user, session, saved, cancel }: { user?: User; session: Session; saved: () => void; cancel: () => void }): ReactNode {
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (form: HTMLFormElement, action: string): Promise<void> => {
    const data = new FormData(form); data.set('csrf_token', csrf()); data.set('action', action); data.set('expected_generation', user?.generation ?? '');
    setBusy(true); setError(null);
    try { await api(user ? `/api/ui/admin/users/${user.user_id}` : '/api/ui/admin/users', feedbackSchema, { method: 'POST', body: data }); saved(); }
    catch (failure) { if (failure instanceof Error) setError(failure); }
    finally { setBusy(false); }
  };
  return <form className="user-editor" onSubmit={event => { event.preventDefault(); void save(event.currentTarget, 'save'); }}><ErrorBox error={error} /><label>DiscordユーザーID<input name={user ? 'replacement_user_id' : 'discord_user_id'} defaultValue={user?.user_id ?? ''} pattern="[1-9][0-9]{16,19}" inputMode="numeric" required autoComplete="off" disabled={busy || session.read_only} /></label><label>権限<select name="role" defaultValue={user?.is_admin ? 'admin' : 'viewer'} disabled={busy || session.read_only}><option value="viewer">一般</option><option value="admin">管理者</option></select></label><div className="form-actions"><button type="button" className="text-button" onClick={cancel}>キャンセル</button>{user && <button type="button" className="text-button danger" disabled={busy || session.read_only} onClick={event => { const form = event.currentTarget.form; if (form && window.confirm(`${user.user_id}の利用許可を削除しますか？`)) void save(form, 'revoke'); }}>削除</button>}<button className="primary-button" disabled={busy || session.read_only}>保存</button></div></form>;
}
export function Users({ session }: { session: Session }): ReactNode {
  const resource = useResource('/api/ui/admin/users', usersSchema);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const saved = (): void => { setEditing(null); setNotice('保存しました。'); resource.reload(); };
  if (resource.loading && !resource.data) return <Skeleton />;
  return <main className="main-shell admin-page"><div className="page-heading"><h1>利用ユーザー</h1><button className="text-button" disabled={session.read_only} onClick={() => setEditing('new')}>追加</button></div><ErrorBox error={resource.error} retry={resource.reload} />{notice && <p role="status">{notice}</p>}{editing === 'new' && <UserEditor session={session} saved={saved} cancel={() => setEditing(null)} />}{resource.data?.entries.map(user => <section className="admin-row" key={user.user_id}>{editing === user.user_id ? <UserEditor user={user} session={session} saved={saved} cancel={() => setEditing(null)} /> : <><span>{user.user_id}</span><span>{user.is_admin ? '管理者' : '一般'}</span>{user.source === 'database' ? <button className="text-button" disabled={session.read_only} onClick={() => setEditing(user.user_id)}>編集</button> : <span className="muted">初期設定</span>}</>}</section>)}</main>;
}
export function Exports(): ReactNode {
  return <main className="main-shell admin-page"><h1>データ出力</h1><section className="admin-row"><h2>店舗一覧</h2><a className="primary-button" href="/export.csv">CSV</a></section><section className="admin-row"><h2>確認記録</h2><a className="primary-button" href="/admin/review/audit.csv">CSV</a></section><Link className="text-button" href="/admin/shops/bulk">一括編集</Link></main>;
}
export function Bulk({ session }: { session: Session }): ReactNode {
  const [file, setFile] = useState<File | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const perform = async (operation: 'validate' | 'apply'): Promise<void> => {
    if (!file) { setError(new Error('CSVを選択してください。')); return; }
    const data = new FormData(); data.set('file', file); data.set('csrf_token', csrf()); setBusy(true); setError(null);
    try { setFeedback(await api(`/api/ui/admin/import/${operation}`, feedbackSchema, { method: 'POST', body: data })); }
    catch (failure) { if (failure instanceof Error) setError(failure); if (operation === 'apply') setFeedback(null); }
    finally { setBusy(false); }
  };
  const submit = (event: FormEvent<HTMLFormElement>): void => { event.preventDefault(); void perform('validate'); };
  return <main className="main-shell admin-page"><h1>一括編集</h1><a className="text-button" href="/admin/import/update/template.csv">編集用CSV</a><form className="admin-section" onSubmit={submit}><label>CSV<input type="file" accept=".csv,text/csv" disabled={busy || session.read_only} onChange={event => { setFile(event.target.files?.[0] ?? null); setFeedback(null); }} /></label><button className="primary-button" disabled={busy || session.read_only}>検証</button></form><ErrorBox error={error} />{feedback?.message && <p role="status">{feedback.message}</p>}{feedback?.preview && <section className="admin-section"><h2>変更内容</h2><p>{feedback.preview.filename} · {feedback.preview.row_count}行 · 更新{feedback.preview.updated}件</p><div className="table-scroll"><table><thead><tr><th>店舗</th><th>項目</th><th>変更前</th><th>変更後</th></tr></thead><tbody>{feedback.changes.map((change, index) => <tr key={index}><td>{change.shop_name}</td><td>{change.field_label}</td><td className="preserve-lines">{change.previous}</td><td className="preserve-lines">{change.proposed}</td></tr>)}</tbody></table></div><button className="primary-button" disabled={busy || session.read_only} onClick={() => { if (window.confirm('表示したCSVの変更内容を適用しますか？')) void perform('apply'); }}>適用</button></section>}</main>;
}
