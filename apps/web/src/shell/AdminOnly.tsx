// The admin guard (10-web-client.md section 4.7): routes under `/admin/` render only for an admin;
// otherwise Admin only replaces the main content. The admin screens are lazy modules rendered
// inside this guard, so a non-admin's browser never downloads admin code. The guard is
// presentation: only the API protects data.

import { Outlet } from "react-router";

import { AdminOnlyScreen } from "../auth/AccessScreens";
import { useCurrentUser } from "./AppShell";

export function AdminOnly() {
  const user = useCurrentUser();
  return user.isAdmin ? <Outlet /> : <AdminOnlyScreen />;
}
