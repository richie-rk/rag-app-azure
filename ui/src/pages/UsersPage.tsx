import { useEffect, useState } from "react";
import {
  makeStyles,
  Text,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Badge,
  Spinner,
} from "@fluentui/react-components";
import { apiClient } from "../api/client";
import type { UserInfo } from "../api/types";

const useStyles = makeStyles({
  root: { padding: "24px", maxWidth: "960px", margin: "0 auto" },
});

export function UsersPage() {
  const styles = useStyles();
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiClient("/users")
      .then(setUsers)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Spinner style={{ padding: 24 }} />;

  return (
    <div className={styles.root}>
      <Text size={600} weight="semibold">
        Users
      </Text>

      <Table style={{ marginTop: 16 }}>
        <TableHeader>
          <TableRow>
            <TableHeaderCell>Name</TableHeaderCell>
            <TableHeaderCell>Email</TableHeaderCell>
            <TableHeaderCell>Role</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {users.map((u) => (
            <TableRow key={u.id}>
              <TableCell>{u.display_name}</TableCell>
              <TableCell>{u.email}</TableCell>
              <TableCell>
                <Badge
                  color={u.role === "admin" ? "brand" : "informative"}
                >
                  {u.role}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
