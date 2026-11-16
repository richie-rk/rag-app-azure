// Indexes (`/admin/indexes`, 10-web-client.md section 7.7): New index, the table in the API's order
// and Show more.

import { TableCell, TableRow, Text, makeStyles, tokens } from "@fluentui/react-components";
import { Add20Regular } from "@fluentui/react-icons";
import { useInfiniteQuery } from "@tanstack/react-query";

import { request } from "../../api/http";
import { apiErrorOf } from "../../api/problem";
import { en } from "../../copy/en";
import { ADMIN_LIST_PAGE_SIZE } from "../../platform/constants";
import { fillText } from "../../ui/format";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { LoadErrorBar } from "../shared/AdminStates";
import { LinkButton } from "../shared/LinkButton";
import { adminListQuery, cursorQuery, itemsOf, type IndexSummary } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

/** `adminListIndexes`, `limit=50` a page, every status except `deleted`. */
export function indexesQuery() {
  return adminListQuery<IndexSummary>("indexes", {}, (cursor, signal) =>
    request("adminListIndexes", {
      query: { limit: ADMIN_LIST_PAGE_SIZE, ...cursorQuery(cursor) },
      signal,
    }),
  );
}

/** `indexes.embeddingValue`: the model and its dimensions. */
export function embeddingText(embedding: IndexSummary["embedding"]): string {
  return fillText(en["indexes.embeddingValue"], {
    model: embedding.model,
    dimensions: embedding.dimensions,
  });
}

export function IndexesScreen() {
  const styles = useStyles();
  useDocumentTitle(en["indexes.title"]);
  const list = useInfiniteQuery(indexesQuery());
  const items = itemsOf(list.data);

  let content;
  if (list.isPending) {
    content = <TableSkeleton />;
  } else if (list.data === undefined) {
    const error = apiErrorOf(list.error);
    content =
      error === null ? null : (
        <LoadErrorBar
          title={en["indexes.loadError"]}
          error={error}
          onRetry={() => {
            void list.refetch();
          }}
        />
      );
  } else if (items.length === 0) {
    content = <Text>{en["indexes.empty"]}</Text>;
  } else {
    content = (
      <>
        <AdminTable
          caption={en["indexes.title"]}
          headers={[
            en["indexes.col.name"],
            en["indexes.col.method"],
            en["indexes.col.embeddingModel"],
            en["indexes.col.documents"],
            en["indexes.col.status"],
          ]}
        >
          {items.map((index) => (
            <TableRow key={index.indexId}>
              <TableCell>
                <RouterLink to={`/admin/indexes/${index.indexId}`}>{index.displayName}</RouterLink>
              </TableCell>
              <TableCell>{index.method.displayName}</TableCell>
              <TableCell>{embeddingText(index.embedding)}</TableCell>
              <TableCell>{index.liveDocumentCount}</TableCell>
              <TableCell>
                <StatusBadge kind="indexStatus" value={index.status} />
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
        title={en["indexes.title"]}
        actions={
          <LinkButton to="/admin/indexes/new" appearance="primary" icon={<Add20Regular />}>
            {en["indexes.new"]}
          </LinkButton>
        }
      />
      {content}
    </div>
  );
}
