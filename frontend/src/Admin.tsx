import { FileInput } from '@astryxdesign/core/FileInput';
import { Card } from '@astryxdesign/core/Card';
import { Button } from '@astryxdesign/core/Button';
import { Table, TableHeader, TableBody, TableRow, TableHeaderCell, TableCell } from '@astryxdesign/core/Table';
import { Banner } from '@astryxdesign/core/Banner';
import { Selector } from '@astryxdesign/core/Selector';
import { TextInput } from '@astryxdesign/core/TextInput';
import type { InputStatus } from '@astryxdesign/core/Field';
import { DropdownMenu } from '@astryxdesign/core/DropdownMenu';
import { EmptyState } from '@astryxdesign/core/EmptyState';
import { Grid } from '@astryxdesign/core/Grid';
import { HStack } from '@astryxdesign/core/HStack';
import { List, ListItem } from '@astryxdesign/core/List';
import { SideNav, SideNavItem } from '@astryxdesign/core/SideNav';
import { Spinner } from '@astryxdesign/core/Spinner';
import { useMediaQuery } from '@astryxdesign/core/hooks';
import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, csrf, failuresSchema, feedbackSchema, jsonBody, reviewSchema, reviewsSchema, usersSchema } from './api';
import type { Feedback, Session, User } from './api';
import { ErrorBox, ExternalLink, Skeleton, navigate, useResource } from './shared';

const adminPages = [
  ['/admin/', '概要'], ['/admin/review', 'データ確認'], ['/admin/shops/bulk', '一括編集'],
  ['/admin/exports', 'データ出力'], ['/admin/users', '利用ユーザー'],
] as const;

export function AdminFrame({ path, readOnly, children }: { path: string; readOnly: boolean; children: ReactNode }): ReactNode {
  const compact = useMediaQuery('(max-width: 700px)');
  const isActive = (href: string): boolean => path === href || (href === '/admin/' && path === '/admin');
  const current = adminPages.find(([href]) => isActive(href))?.[1] ?? '管理';
  return <div className="admin-workspace">
    {compact
      ? <DropdownMenu button={{ label: `管理：${current}`, variant: 'secondary', width: '100%' }} items={adminPages.map(([href, label]) => ({ label, onClick: () => navigate(href) }))} />
      : <SideNav className="admin-sidebar" header={<h2>管理</h2>}>{adminPages.map(([href, label]) =>
        <SideNavItem key={href} href={href} label={label} isSelected={isActive(href)} onClick={event => { event.preventDefault(); navigate(href); }} />)}</SideNav>}
    <div className="admin-body">{readOnly && <Banner className="read-only-banner" status="warning" title="読取専用" />}{children}</div>
  </div>;
}
function Failures({ session }: { session: Session }): ReactNode {
  const [cursor, setCursor] = useState('');
  const resource = useResource('/api/admin/review-failures?active_only=true' + (cursor ? '&cursor=' + cursor : ''), failuresSchema);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  return <Card className="admin-section" padding={4}><h2>登録できなかった投稿</h2><ErrorBox error={resource.error ?? error} retry={resource.reload} />{resource.loading && <Spinner label="読み込み中" />}{resource.data && resource.data.items.length > 0 && <List hasDividers>{resource.data.items.map(item => <ListItem key={item.message_id} label={<span className="preserve-lines">{item.message_content}</span>} description={<><span className="field-error">{item.error}</span><br /><ExternalLink href={item.discord_url}>Discord</ExternalLink></>} endContent={item.review_mention_id ? <Button variant="primary" label="確認" onClick={() => navigate(`/admin/review?mention=${item.review_mention_id}`)} /> : <Button variant="primary" label="確認項目を作成" isDisabled={busy || session.read_only} onClick={() => { setBusy(true); setError(null); api(`/api/admin/review-failures/${item.message_id}/prepare`, reviewSchema, jsonBody({})).then(result => navigate(`/admin/review?mention=${result.id}`), (failure: Error) => { setError(failure); setBusy(false); }); }} />} />)}</List>}{resource.data?.items.length === 0 && <EmptyState title="対象の投稿はありません" isCompact />}{resource.data?.next_cursor && <Button variant="ghost" label="次へ" onClick={() => setCursor(resource.data?.next_cursor ?? '')} />}</Card>;
}
export function Overview({ session }: { session: Session }): ReactNode {
  const resource = useResource('/api/admin/reviews?status=unresolved&limit=1', reviewsSchema);
  return <main className="admin-screen"><h1>概要</h1><ErrorBox error={resource.error} retry={resource.reload} /><Card className="admin-summary" padding={4}><div className="admin-summary-count"><h2>未確認</h2><strong>{resource.data?.counters.unresolved ?? '—'}</strong></div><Button href="/admin/review" variant="primary" label="データ確認" /></Card><Failures session={session} /></main>;
}
function UserEditor({ user, session, saved, cancel }: { user?: User; session: Session; saved: (message: string) => void; cancel: () => void }): ReactNode {
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [userId, setUserId] = useState(user?.user_id ?? '');
  const [role, setRole] = useState(user?.is_admin ? 'admin' : 'viewer');
  const [idStatus, setIdStatus] = useState<InputStatus | undefined>(undefined);
  const save = async (form: HTMLFormElement, action: string): Promise<void> => {
    // Astryx inputs do not set native pattern, so check the Discord ID here.
    if (!/^[1-9][0-9]{16,19}$/.test(userId)) { setIdStatus({ type: 'error', message: 'DiscordユーザーIDは17〜20桁の数字で入力してください。' }); return; }
    setIdStatus(undefined);
    const data = new FormData(form); data.set('csrf_token', csrf()); data.set('action', action); data.set('expected_generation', user?.generation ?? '');
    setBusy(true); setError(null);
    try { await api(user ? `/api/ui/admin/users/${user.user_id}` : '/api/ui/admin/users', feedbackSchema, { method: 'POST', body: data }); saved(action === 'revoke' ? '削除しました。' : '保存しました。'); }
    catch (failure) { if (failure instanceof Error) setError(failure); }
    finally { setBusy(false); }
  };
  return <form className="admin-user-form" onSubmit={event => { event.preventDefault(); void save(event.currentTarget, 'save'); }}><ErrorBox error={error} /><Grid columns={{ minWidth: 200, max: 2 }} gap={3}><TextInput label="DiscordユーザーID" htmlName={user ? 'replacement_user_id' : 'discord_user_id'} value={userId} onChange={setUserId} inputMode="numeric" isRequired autoComplete="off" status={idStatus} isDisabled={busy || session.read_only} /><Selector label="権限" htmlName="role" options={[{ value: 'viewer', label: '一般' }, { value: 'admin', label: '管理者' }]} value={role} onChange={setRole} isDisabled={busy || session.read_only} /></Grid><HStack justify="end" gap={3} className="form-actions"><Button type="button" variant="ghost" label="キャンセル" onClick={cancel} />{user && <Button type="button" variant="destructive" label="削除" isDisabled={busy || session.read_only} onClick={event => { const form = event.currentTarget.form; if (form) void save(form, 'revoke'); }} />}<Button type="submit" variant="primary" label="保存" isLoading={busy} isDisabled={session.read_only} /></HStack></form>;
}
export function Users({ session }: { session: Session }): ReactNode {
  const resource = useResource('/api/ui/admin/users', usersSchema);
  const [editing, setEditing] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const editedUser = resource.data?.entries.find(user => user.user_id === editing);
  const saved = (message: string): void => { setEditing(null); setNotice(message); resource.reload(); };
  if (resource.loading && !resource.data) return <Skeleton />;
  return <main className="admin-screen"><HStack className="page-heading" justify="between" gap={4}><h1>利用ユーザー</h1><Button variant="primary" label="追加" isDisabled={session.read_only} onClick={() => setEditing('new')} /></HStack><ErrorBox error={resource.error} retry={resource.reload} />{notice && <Banner status="success" title={notice} />}{(editing === 'new' || editedUser) && <Card padding={4}><h2>{editing === 'new' ? '利用ユーザーを追加' : '利用ユーザーを編集'}</h2><UserEditor key={editing} user={editedUser} session={session} saved={saved} cancel={() => setEditing(null)} /></Card>}<Card className="admin-user-list" padding={0}><div className="table-scroll"><Table aria-label="登録済みユーザー"><TableHeader><TableRow><TableHeaderCell>DiscordユーザーID</TableHeaderCell><TableHeaderCell>権限</TableHeaderCell><TableHeaderCell>操作</TableHeaderCell></TableRow></TableHeader><TableBody>{resource.data?.entries.map(user => <TableRow key={user.user_id}><TableCell className="admin-user-id">{user.user_id}</TableCell><TableCell>{user.is_admin ? '管理者' : '一般'}</TableCell><TableCell>{user.source === 'database' ? <Button variant="ghost" size="sm" label="編集" isDisabled={session.read_only} onClick={() => setEditing(user.user_id)} /> : <span className="muted">初期設定</span>}</TableCell></TableRow>)}</TableBody></Table></div></Card></main>;
}
const csvExports = [
  { title: '店舗一覧', href: '/export.csv' },
  { title: '確認記録', href: '/admin/review/audit.csv' },
] as const;

export function Exports(): ReactNode {
  return <main className="admin-screen">
    <h1>データ出力</h1>
    <Card className="admin-exports" padding={2}>
      <List hasDividers>{csvExports.map(({ title, href }) => <ListItem key={href} label={title} endContent={<Button href={href} variant="secondary" label={title + 'のCSVを取得'}>CSVを取得</Button>} />)}</List>
    </Card>
  </main>;
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
  return <main className="admin-screen">
    <h1>一括編集</h1>
    <Card padding={4}>
      <h2>1. 編集用CSVを取得</h2>
      <HStack justify="between" align="center" gap={4} wrap="wrap">
        <p>現在の店舗一覧をCSVで取得し、変更したい項目を書き換えます。</p>
        <Button href="/admin/import/update/template.csv" variant="secondary" label="編集用CSVを取得" />
      </HStack>
    </Card>
    <Card padding={4}>
      <h2>2. 編集済みCSVを検証</h2>
      <form className="admin-bulk-upload" onSubmit={submit}>
        <FileInput label="編集済みCSV" mode="dropzone" accept=".csv,text/csv" value={file} isDisabled={busy || session.read_only} onChange={selection => {
          setFile(Array.isArray(selection) ? selection[0] ?? null : selection);
          setFeedback(null);
          setError(null);
        }} />
        {file && <HStack justify="end">
          <Button type="submit" variant="primary" label="検証" isLoading={busy} isDisabled={session.read_only} />
        </HStack>}
        <ErrorBox error={error} />
      </form>
    </Card>
    {feedback?.message && <Banner status="success" title={feedback.message} />}{feedback?.preview && <Card className="admin-section" padding={4}><h2>3. 変更内容を確認して適用</h2><p>{feedback.preview.filename} · {feedback.preview.row_count}行 · 更新{feedback.preview.updated}件</p><div className="table-scroll"><Table><TableHeader><TableRow><TableHeaderCell>店舗</TableHeaderCell><TableHeaderCell>項目</TableHeaderCell><TableHeaderCell>変更前</TableHeaderCell><TableHeaderCell>変更後</TableHeaderCell></TableRow></TableHeader><TableBody>{feedback.changes.map((change, index) => <TableRow key={index}><TableCell>{change.shop_name}</TableCell><TableCell>{change.field_label}</TableCell><TableCell className="preserve-lines">{change.previous}</TableCell><TableCell className="preserve-lines">{change.proposed}</TableCell></TableRow>)}</TableBody></Table></div><HStack justify="end"><Button variant="primary" label="変更を適用" isLoading={busy} isDisabled={session.read_only} onClick={() => void perform('apply')} /></HStack></Card>}</main>;
}
