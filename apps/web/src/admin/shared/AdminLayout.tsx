// The layout route around every admin screen: the admin toaster and the routed screen. It is a lazy
// module inside the admin guard, so a non-admin's browser never loads it (10-web-client.md section
// 4.7), and it stays mounted while an admin moves between admin screens, so a toast survives the
// navigation after a create or a delete.

import { Outlet } from "react-router";

import { AdminToaster } from "./toast";

export function AdminLayout() {
  return (
    <>
      <Outlet />
      <AdminToaster />
    </>
  );
}
