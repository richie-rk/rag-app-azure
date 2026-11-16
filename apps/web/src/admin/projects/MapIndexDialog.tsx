// Map an index (10-web-client.md section 7.6): a dialog with the indexes that aren't mapped to this
// project and whose status is mappable, and Make this the default. Offering only mappable indexes
// means a project never gains a default that can't serve. On a `412` the dialog stays open with
// its choices.

import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Dropdown,
  Field,
  Option,
  Spinner,
  Switch,
  Text,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { apiErrorOf, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { FocusFirstControl } from "../../ui/FocusFirstControl";
import { allIndexesQuery, type IndexStatus, type IndexSummary } from "../shared/queries";
import { StatusBadge } from "../shared/StatusBadge";

const useStyles = makeStyles({
  content: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  option: { display: "flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
});

/** The statuses `adminMapProjectIndex` accepts (`11-api.md` section 4.8). */
export const MAPPABLE_STATUSES: ReadonlySet<IndexStatus> = new Set([
  "provisioning",
  "ready",
  "reingest_required",
  "reingesting",
  "rebuilding",
]);

/** The indexes the dialog offers: not mapped to the project, with a mappable status. */
export function mappableIndexes(
  indexes: readonly IndexSummary[],
  mappedIds: ReadonlySet<string>,
): IndexSummary[] {
  return indexes.filter(
    (index) => !mappedIds.has(index.indexId) && MAPPABLE_STATUSES.has(index.status),
  );
}

export interface MapIndexDialogProps {
  readonly open: boolean;
  /** The indexes the project maps now. */
  readonly mappedIds: ReadonlySet<string>;
  /** Whether the project has no mapping yet: its first index must be the default. */
  readonly firstMapping: boolean;
  readonly busy: boolean;
  /** The last refusal, shown in the dialog with its lines. */
  readonly failure: { readonly error: ApiError; readonly lines: readonly string[] } | null;
  /** Map stays disabled until a `429`'s time passes. */
  readonly waiting: boolean;
  readonly onMap: (index: IndexSummary, isDefault: boolean) => void;
  readonly onClose: () => void;
}

export function MapIndexDialog(props: MapIndexDialogProps) {
  // The choices live as long as one opening: a new opening starts from the defaults.
  return props.open ? <MapIndexDialogOpen {...props} /> : null;
}

function MapIndexDialogOpen({
  mappedIds,
  firstMapping,
  busy,
  failure,
  waiting,
  onMap,
  onClose,
}: MapIndexDialogProps) {
  const styles = useStyles();
  const indexes = useQuery(allIndexesQuery());
  const choices = mappableIndexes(indexes.data ?? [], mappedIds);
  const [chosenId, setChosenId] = useState<string | null>(null);
  const [makeDefault, setMakeDefault] = useState(firstMapping);
  const chosen = choices.find((index) => index.indexId === chosenId) ?? null;
  const loadError = indexes.isError ? apiErrorOf(indexes.error) : null;

  let picker;
  if (indexes.isPending) {
    picker = <Spinner size="tiny" aria-label={en["projectIndexes.picker"]} />;
  } else if (loadError !== null) {
    picker = (
      <ApiErrorBar
        error={loadError}
        onRetry={() => {
          void indexes.refetch();
        }}
      />
    );
  } else if (choices.length === 0) {
    picker = <Text>{en["projectIndexes.noneAvailable"]}</Text>;
  } else {
    picker = (
      <Field label={en["projectIndexes.picker"]}>
        <Dropdown
          value={chosen?.displayName ?? ""}
          selectedOptions={chosen === null ? [] : [chosen.indexId]}
          onOptionSelect={(_, data) => {
            setChosenId(data.optionValue ?? null);
          }}
        >
          {choices.map((index) => (
            <Option key={index.indexId} value={index.indexId} text={index.displayName}>
              <span className={styles.option}>
                {index.displayName}
                <StatusBadge kind="indexStatus" value={index.status} />
              </span>
            </Option>
          ))}
        </Dropdown>
      </Field>
    );
  }

  return (
    <Dialog
      open
      modalType="modal"
      onOpenChange={(_, data) => {
        if (!data.open && !busy) onClose();
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{en["projectIndexes.map"]}</DialogTitle>
          <DialogContent className={styles.content}>
            {picker}
            <Switch
              label={en["projectIndexes.makeDefaultSwitch"]}
              checked={makeDefault}
              onChange={(_, data) => {
                setMakeDefault(data.checked);
              }}
            />
            {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
          </DialogContent>
          <DialogActions>
            <Button
              appearance="primary"
              disabled={chosen === null || busy || waiting}
              {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={() => {
                if (chosen !== null) onMap(chosen, makeDefault);
              }}
            >
              {en["projectIndexes.mapAction"]}
            </Button>
            <Button onClick={onClose} disabled={busy}>
              {en["confirm.cancel"]}
            </Button>
          </DialogActions>
          <FocusFirstControl />
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
