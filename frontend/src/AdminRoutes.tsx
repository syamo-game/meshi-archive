import './admin-workspace.css';
import type { ReactNode } from 'react';
import type { Session } from './api';
import { AdminFrame, Bulk, Exports, Overview, Users } from './Admin';
import { Reviews } from './Review';

// Admin screens live in their own chunk so normal viewers do not download them.
export default function AdminRoutes({ session, path, location }: { session: Session; path: string; location: string }): ReactNode {
  let page: ReactNode;
  if (path === '/admin/users') page = <Users session={session} />;
  else if (path === '/admin/exports') page = <Exports />;
  else if (path === '/admin/shops/bulk') page = <Bulk session={session} />;
  else if (path === '/admin/review') page = <Reviews session={session} location={location} />;
  else page = <Overview session={session} />;
  return <AdminFrame path={path} readOnly={session.read_only}>{page}</AdminFrame>;
}
