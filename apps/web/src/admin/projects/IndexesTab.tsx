// The Indexes tab of a project (10-web-client.md section 7.6): the project first, then its index
// mappings; Map an index, Make default and Unmap, each sent with the project's version, keeping the
// `projectRowVersion` it returns and reloading the tab.

import {
  Badge,
  Button,
  TableCell,
  TableRow,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { Add20Regular, LinkDismiss20Regular, Star20Regular } from "@fluentui/react-icons";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { requestWithStatus } from "../../api/http";
import { apiErrorOf, type ApiError } from "../../api/problem";
import { applyProjectRowVersion } from "../../api/queryClient";
import { en } from "../../copy/en";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { useRetryWaits } from "../../ui/retryWait";
import { RouterLink } from "../../ui/RouterLink";
import { AdminTable, RowActions, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { ReloadErrorBar } from "../shared/AdminStates";
import { problemLines } from "../shared/formErrors";
import {
  adminKeys,
  itemsOf,
  type AdminProject,
  type IndexSummary,
  type ProjectIndexMapping,
} from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";
import { useAdminToast } from "../shared/toast";
import { MapIndexDialog } from "./MapIndexDialog";
import { projectIndexesTabQuery } from "./tabQueries";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
});

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
}

function failureOf(error: ApiError): Failure {
  return {
    error,
    lines: error.status === 412 ? [en["admin.conflict.action"]] : problemLines(error),
  };
}

export interface IndexesTabProps {
  readonly project: AdminProject;
  /** A change sent `sent` and returned `next` (section 5.6 step 4, for the Details form). */
  readonly onVersionMoved: (sent: string, next: string) => void;
}

export function IndexesTab({ project, onVersionMoved }: IndexesTabProps) {
  const styles = useStyles();
  const projectId = project.projectId;
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const list = useInfiniteQuery({
    ...projectIndexesTabQuery(queryClient, projectId),
    refetchOnMount: "always",
  });
  const mappings = itemsOf(list.data);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [mapOpen, setMapOpen] = useState(false);
  const [mapFailure, setMapFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const [unmapping, setUnmapping] = useState<ProjectIndexMapping | null>(null);

  const currentVersion = () =>
    queryClient.getQueryData<AdminProject>(adminKeys.project(projectId))?.rowVersion ??
    project.rowVersion;

  /** Sends one mapping change; on success keeps the version, toasts and reloads the tab. */
  const change = async (
    key: string,
    send: (ifMatch: string) => Promise<{ readonly projectRowVersion: string }>,
    done: string,
    onFailure: (failure: Failure) => void,
  ): Promise<boolean> => {
    const sent = currentVersion();
    setBusy(true);
    try {
      const answer = await send(sent);
      applyProjectRowVersion(queryClient, projectId, answer.projectRowVersion);
      onVersionMoved(sent, answer.projectRowVersion);
      toast(done);
      void list.refetch();
      return true;
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return false;
      waits.waitFor(key, error);
      onFailure(failureOf(error));
      // A 412 reloads the tab, project first, so the repeated action sends the reloaded version.
      if (error.status === 412) void list.refetch();
      return false;
    } finally {
      setBusy(false);
    }
  };

  const map = async (index: IndexSummary, isDefault: boolean) => {
    setMapFailure(null);
    const mapped = await change(
      "map",
      async (ifMatch) =>
        (
          await requestWithStatus("adminMapProjectIndex", {
            path: { projectId, indexId: index.indexId },
            body: { isDefault },
            ifMatch,
          })
        ).data,
      en["projectIndexes.mapped"],
      setMapFailure,
    );
    if (mapped) setMapOpen(false);
  };

  const makeDefault = (mapping: ProjectIndexMapping) => {
    setFailure(null);
    void change(
      `default:${mapping.indexId}`,
      async (ifMatch) =>
        (
          await requestWithStatus("adminMapProjectIndex", {
            path: { projectId, indexId: mapping.indexId },
            body: { isDefault: true },
            ifMatch,
          })
        ).data,
      en["projectIndexes.defaultSet"],
      setFailure,
    );
  };

  const unmap = async (mapping: ProjectIndexMapping) => {
    setFailure(null);
    await change(
      `unmap:${mapping.indexId}`,
      async (ifMatch) =>
        (
          await requestWithStatus("adminUnmapProjectIndex", {
            path: { projectId, indexId: mapping.indexId },
            ifMatch,
          })
        ).data,
      en["projectIndexes.unmapped"],
      setFailure,
    );
    setUnmapping(null);
  };

  let table;
  if (list.isPending) {
    table = <TableSkeleton />;
  } else if (list.data === undefined) {
    const error = apiErrorOf(list.error);
    table =
      error === null ? null : (
        <ApiErrorBar
          error={error}
          onRetry={() => {
            void list.refetch();
          }}
        />
      );
  } else {
    table = (
      <>
        <AdminTable
          caption={en["project.tabs.indexes"]}
          headers={[
            en["projectIndexes.col.index"],
            en["projectIndexes.col.default"],
            en["projectIndexes.col.status"],
            "",
          ]}
        >
          {mappings.map((mapping) => (
            <TableRow key={mapping.indexId}>
              <TableCell>
                <RouterLink to={`/admin/indexes/${mapping.indexId}`}>
                  {mapping.displayName}
                </RouterLink>
              </TableCell>
              <TableCell>
                {mapping.isDefault ? (
                  <Badge appearance="tint" color="brand">
                    {en["projectIndexes.defaultBadge"]}
                  </Badge>
                ) : null}
              </TableCell>
              <TableCell>
                <StatusBadge kind="indexStatus" value={mapping.indexStatus} />
              </TableCell>
              <TableCell>
                <RowActions
                  name={mapping.displayName}
                  actions={[
                    ...(mapping.isDefault
                      ? []
                      : [
                          {
                            key: "default",
                            label: en["projectIndexes.makeDefault"],
                            icon: <Star20Regular />,
                            disabled: busy || waits.isWaiting(`default:${mapping.indexId}`),
                            onClick: () => {
                              makeDefault(mapping);
                            },
                          },
                        ]),
                    {
                      key: "unmap",
                      label: en["projectIndexes.unmap"],
                      icon: <LinkDismiss20Regular />,
                      disabled: busy || waits.isWaiting(`unmap:${mapping.indexId}`),
                      onClick: () => {
                        setUnmapping(mapping);
                      },
                    },
                  ]}
                />
              </TableCell>
            </TableRow>
          ))}
        </AdminTable>
        {mappings.length === 0 ? <Text>{en["projectIndexes.empty"]}</Text> : null}
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
      <div>
        <Button
          icon={<Add20Regular />}
          disabled={list.data === undefined}
          onClick={() => {
            setMapFailure(null);
            setMapOpen(true);
          }}
        >
          {en["projectIndexes.map"]}
        </Button>
      </div>
      {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
      <ReloadErrorBar list={list} />
      {table}
      <MapIndexDialog
        open={mapOpen}
        mappedIds={new Set(mappings.map((mapping) => mapping.indexId))}
        firstMapping={mappings.length === 0}
        busy={busy}
        failure={mapFailure}
        waiting={waits.isWaiting("map")}
        onMap={(index, isDefault) => {
          void map(index, isDefault);
        }}
        onClose={() => {
          setMapOpen(false);
        }}
      />
      <ConfirmDialog
        open={unmapping !== null}
        title={fillText(en["projectIndexes.unmapConfirm.title"], {
          index: unmapping?.displayName ?? "",
          project: project.name,
        })}
        body={en["projectIndexes.unmapConfirm.body"]}
        actionLabel={en["projectIndexes.unmapConfirm.action"]}
        busy={busy}
        onConfirm={() => {
          if (unmapping !== null) void unmap(unmapping);
        }}
        onCancel={() => {
          setUnmapping(null);
        }}
      />
    </div>
  );
}
