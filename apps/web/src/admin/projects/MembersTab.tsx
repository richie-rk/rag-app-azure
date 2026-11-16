// The Members tab (10-web-client.md section 7.6): the project first, then the members; Add people
// with its one-person-at-a-time loop; Remove per row. Every change sends the project's version and
// keeps the `projectRowVersion` it returns, so one admin's changes chain without a reload.

import {
  Button,
  Spinner,
  TableCell,
  TableRow,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { PeopleAdd20Regular, PersonDelete20Regular } from "@fluentui/react-icons";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { request } from "../../api/http";
import { apiErrorOf, type ApiError } from "../../api/problem";
import { applyProjectRowVersion } from "../../api/queryClient";
import { en } from "../../copy/en";
import { ApiErrorBar, ErrorBar } from "../../ui/ErrorBar";
import { fillText, formatDate, formatRetryTime, pluralText } from "../../ui/format";
import { useRetryWaits } from "../../ui/retryWait";
import { ConfirmDialog } from "../../ui/ConfirmDialog";
import { AdminTable, RowActions, ShowMore, TableSkeleton } from "../shared/AdminTable";
import { ReloadErrorBar } from "../shared/AdminStates";
import { problemLines } from "../shared/formErrors";
import { PeoplePicker, personName } from "../shared/PeoplePicker";
import {
  adminKeys,
  itemsOf,
  type AdminProject,
  type ProjectMember,
  type UserSummary,
} from "../shared/queries";
import { useAdminToast } from "../shared/toast";
import { addPeople } from "./addPeople";
import { membersTabQuery } from "./tabQueries";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  add: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalS,
    maxWidth: "640px",
  },
  addRow: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalM,
    rowGap: tokens.spacingVerticalS,
  },
});

const names = new Intl.ListFormat("en", { style: "long", type: "conjunction" });

/** `members.col.addedBy`: who added the person, or how they joined. */
export function addedByText(member: ProjectMember): string {
  if (member.grantedBy === null) return en["members.addedByDefault"];
  return member.grantedBy.displayName === ""
    ? en["members.addedByAdmin"]
    : member.grantedBy.displayName;
}

/** The row that takes focus after `userId`'s row is removed: the next one, else the one before,
 * else none (the caption). */
export function rowAfterRemoval(userIds: readonly string[], userId: string): string | null {
  const index = userIds.indexOf(userId);
  const rest = userIds.filter((each) => each !== userId);
  if (rest.length === 0 || index < 0) return null;
  return rest[Math.min(index, rest.length - 1)] ?? null;
}

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
}

export interface MembersTabProps {
  readonly project: AdminProject;
  /** A change sent `sent` and returned `next` (section 5.6 step 4, for the Details form). */
  readonly onVersionMoved: (sent: string, next: string) => void;
}

export function MembersTab({ project, onVersionMoved }: MembersTabProps) {
  const styles = useStyles();
  const projectId = project.projectId;
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const list = useInfiniteQuery({
    ...membersTabQuery(queryClient, projectId),
    refetchOnMount: "always",
  });
  const members = itemsOf(list.data);
  const captionRef = useRef<HTMLTableCaptionElement>(null);
  const pendingFocus = useRef<string | null | undefined>(undefined);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [removing, setRemoving] = useState<ProjectMember | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [picked, setPicked] = useState<UserSummary[]>([]);
  const [running, setRunning] = useState(false);
  const [waitingUntil, setWaitingUntil] = useState<number | null>(null);
  const [notAdded, setNotAdded] = useState<UserSummary[]>([]);
  const loop = useRef<AbortController | null>(null);

  // Leaving the tab ends the loop before its next request.
  useEffect(
    () => () => {
      loop.current?.abort();
    },
    [],
  );

  // After a removal, once the reloaded rows are on screen, focus moves to the next row's name, or
  // to the caption when no row remains (section 11.4).
  useEffect(() => {
    if (pendingFocus.current === undefined || list.isFetching) return;
    const userId = pendingFocus.current;
    pendingFocus.current = undefined;
    const row =
      userId === null
        ? null
        : document.querySelector<HTMLElement>(`[data-member-name="${userId}"]`);
    (row ?? captionRef.current)?.focus();
  });

  /** The version every change sends: the cached project's, which the tab's last load wrote. */
  const currentVersion = () =>
    queryClient.getQueryData<AdminProject>(adminKeys.project(projectId))?.rowVersion ??
    project.rowVersion;

  const keepVersion = (sent: string, next: string) => {
    applyProjectRowVersion(queryClient, projectId, next);
    onVersionMoved(sent, next);
  };

  const failed = (error: ApiError) => {
    setFailure({
      error,
      lines: error.status === 412 ? [en["admin.conflict.action"]] : problemLines(error),
    });
  };

  const runAdd = async () => {
    const controller = new AbortController();
    loop.current = controller;
    setRunning(true);
    setFailure(null);
    setNotAdded([]);
    const result = await addPeople({
      projectId,
      people: picked,
      rowVersion: currentVersion(),
      signal: controller.signal,
      onAdded: (person, change, sent) => {
        keepVersion(sent, change.projectRowVersion);
        setPicked((current) => current.filter((each) => each.userId !== person.userId));
      },
      onWaiting: setWaitingUntil,
    });
    if (result.aborted) return;
    setRunning(false);
    setWaitingUntil(null);
    if (result.added.length > 0) toast(pluralText(en["members.added"], result.added.length));
    setNotAdded([...result.failed]);
    if (result.stoppedBy !== null) failed(result.stoppedBy);
    await list.refetch();
  };

  const remove = async (member: ProjectMember) => {
    const sent = currentVersion();
    setRemoveBusy(true);
    setFailure(null);
    try {
      const answer = await request("adminRemoveProjectMember", {
        path: { projectId, userId: member.userId },
        ifMatch: sent,
      });
      keepVersion(sent, answer.projectRowVersion);
      toast(fillText(en["members.removed"], { name: personName(member) }));
      pendingFocus.current = rowAfterRemoval(
        members.map((each) => each.userId),
        member.userId,
      );
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") {
        waits.waitFor(`remove:${member.userId}`, error);
        failed(error);
      }
    } finally {
      setRemoveBusy(false);
      setRemoving(null);
    }
    await list.refetch();
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
          caption={en["project.tabs.members"]}
          captionRef={captionRef}
          headers={[
            en["members.col.name"],
            en["members.col.email"],
            en["members.col.addedBy"],
            en["members.col.added"],
            "",
          ]}
        >
          {members.map((member) => {
            const name = personName(member);
            return (
              <TableRow key={member.userId}>
                <TableCell>
                  <span tabIndex={-1} data-member-name={member.userId}>
                    {name}
                  </span>
                </TableCell>
                <TableCell>{member.email}</TableCell>
                <TableCell>{addedByText(member)}</TableCell>
                <TableCell>{formatDate(member.grantedAt)}</TableCell>
                <TableCell>
                  <RowActions
                    name={name}
                    actions={[
                      {
                        key: "remove",
                        label: en["members.remove"],
                        accessibleName: fillText(en["members.removeName"], { name }),
                        icon: <PersonDelete20Regular />,
                        disabled: waits.isWaiting(`remove:${member.userId}`),
                        onClick: () => {
                          setRemoving(member);
                        },
                      },
                    ]}
                  />
                </TableCell>
              </TableRow>
            );
          })}
        </AdminTable>
        {members.length === 0 ? <Text>{en["members.empty"]}</Text> : null}
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

  const removingName = removing === null ? "" : personName(removing);

  return (
    <div className={styles.root}>
      <div>
        <Button
          icon={<PeopleAdd20Regular />}
          aria-expanded={adding}
          onClick={() => {
            setAdding((open) => !open);
          }}
        >
          {en["members.add"]}
        </Button>
      </div>
      {adding ? (
        <div className={styles.add}>
          <PeoplePicker
            label={en["members.picker.label"]}
            placeholder={en["members.picker.placeholder"]}
            projectId={projectId}
            selected={picked}
            disabled={running}
            onChange={setPicked}
          />
          <div className={styles.addRow}>
            <Button
              appearance="primary"
              disabled={running || picked.length === 0}
              {...(running ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={() => {
                void runAdd();
              }}
            >
              {en["members.addAction"]}
            </Button>
            {waitingUntil === null ? null : (
              <Text>
                {fillText(en["members.waiting"], { time: formatRetryTime(waitingUntil) })}
              </Text>
            )}
          </div>
          {notAdded.length === 0 ? null : (
            <ErrorBar
              title={fillText(en["members.addFailed"], {
                names: names.format(notAdded.map(personName)),
              })}
            />
          )}
        </div>
      ) : null}
      {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
      <ReloadErrorBar list={list} />
      {table}
      <ConfirmDialog
        open={removing !== null}
        title={fillText(en["members.removeConfirm.title"], {
          name: removingName,
          project: project.name,
        })}
        body={en["members.removeConfirm.body"]}
        actionLabel={en["members.removeConfirm.action"]}
        busy={removeBusy}
        onConfirm={() => {
          if (removing !== null) void remove(removing);
        }}
        onCancel={() => {
          setRemoving(null);
        }}
      />
    </div>
  );
}
