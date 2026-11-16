// Projects (`/admin/projects`, 10-web-client.md section 7.5): the filter row (`q`, `status`), the
// table in the API's order and Show more. The list isn't polled.

import { TableCell, TableRow, Text, makeStyles, tokens } from "@fluentui/react-components";
import { Add20Regular } from "@fluentui/react-icons";
import { useInfiniteQuery } from "@tanstack/react-query";

import { request } from "../../api/http";
import { projectCreateName } from "../../api/generated/limits.gen";
import { apiErrorOf } from "../../api/problem";
import { en } from "../../copy/en";
import { ADMIN_LIST_PAGE_SIZE } from "../../platform/constants";
import { codePointLength } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { LoadErrorBar } from "../shared/AdminStates";
import { LinkButton } from "../shared/LinkButton";
import {
  FilterDropdown,
  FilterRow,
  FilterSearchBox,
  useUrlFilters,
  type UrlFilter,
} from "../shared/filters";
import { adminListQuery, cursorQuery, itemsOf, type AdminProjectSummary } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

const STATUSES = ["active", "archived", "all"] as const;
type StatusFilter = (typeof STATUSES)[number];

const FILTERS: readonly UrlFilter[] = [
  {
    name: "q",
    allowed: (value) =>
      value.trim() !== "" && codePointLength(value) <= projectCreateName.maxLength,
    fallback: "",
  },
  { name: "status", allowed: STATUSES, fallback: "active" },
];

/** `adminListProjects` for one filter, `limit=50` a page. */
export function projectsQuery(q: string, status: StatusFilter) {
  return adminListQuery<AdminProjectSummary>("projects", { q, status }, (cursor, signal) =>
    request("adminListProjects", {
      query: {
        limit: ADMIN_LIST_PAGE_SIZE,
        status,
        ...(q === "" ? {} : { q }),
        ...cursorQuery(cursor),
      },
      signal,
    }),
  );
}

export function ProjectsScreen() {
  const styles = useStyles();
  useDocumentTitle(en["projects.title"]);
  const { values, setFilter } = useUrlFilters(FILTERS);
  const q = values["q"] ?? "";
  const status = (values["status"] ?? "active") as StatusFilter;
  const list = useInfiniteQuery(projectsQuery(q, status));
  const items = itemsOf(list.data);
  // The default view hides nothing an admin has narrowed; any other filter can hide every row.
  const filtered = q !== "" || status === "archived";

  let content;
  if (list.isPending) {
    content = <TableSkeleton />;
  } else if (list.data === undefined) {
    const error = apiErrorOf(list.error);
    content =
      error === null ? null : (
        <LoadErrorBar
          title={en["projects.loadError"]}
          error={error}
          onRetry={() => {
            void list.refetch();
          }}
        />
      );
  } else if (items.length === 0) {
    content = <Text>{filtered ? en["projects.noMatches"] : en["projects.empty"]}</Text>;
  } else {
    content = (
      <>
        <AdminTable
          caption={en["projects.title"]}
          headers={[
            en["projects.col.name"],
            en["projects.col.defaultIndex"],
            en["projects.col.members"],
            en["projects.col.newUsersJoin"],
            en["projects.col.status"],
          ]}
        >
          {items.map((project) => (
            <TableRow key={project.projectId}>
              <TableCell>
                <RouterLink to={`/admin/projects/${project.projectId}`}>{project.name}</RouterLink>
              </TableCell>
              <TableCell>{project.defaultIndex?.displayName ?? en["projects.noIndex"]}</TableCell>
              <TableCell>{project.memberCount}</TableCell>
              <TableCell>{project.isDefault ? en["projects.yes"] : en["projects.no"]}</TableCell>
              <TableCell>
                <StatusBadge kind="projectStatus" value={project.status} />
              </TableCell>
            </TableRow>
          ))}
        </AdminTable>
        <ShowMore
          hasMore={list.hasNextPage}
          loading={list.isFetchingNextPage}
          error={list.isFetchNextPageError ? apiErrorOf(list.error) : null}
          onShowMore={() => {
            void list.fetchNextPage();
          }}
        />
      </>
    );
  }

  return (
    <div className={styles.root}>
      <PageHeader
        title={en["projects.title"]}
        actions={
          <LinkButton to="/admin/projects/new" appearance="primary" icon={<Add20Regular />}>
            {en["projects.new"]}
          </LinkButton>
        }
      />
      <FilterRow>
        <FilterSearchBox
          label={en["projects.filter"]}
          value={q}
          maxLength={projectCreateName.maxLength}
          onSearch={(value) => {
            setFilter("q", value);
          }}
        />
        <FilterDropdown
          text={en["projects.statusFilter"]}
          values={STATUSES}
          value={status}
          onChange={(value) => {
            setFilter("status", value);
          }}
        />
      </FilterRow>
      {content}
    </div>
  );
}
