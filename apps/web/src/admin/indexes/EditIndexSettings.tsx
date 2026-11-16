// Edit settings on the Index page (10-web-client.md sections 7.4 and 7.8): the New index form
// prefilled with the index's values; changing a setting shows `index.changeWarning` above Save;
// Save changes sends only the changed fields with the index's `rowVersion` and follows the
// edit-conflict steps of section 7.4.

import { Button, Spinner, makeStyles, tokens } from "@fluentui/react-components";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { fillText } from "../../ui/format";
import { useRetryWaits } from "../../ui/retryWait";
import { RouterLink } from "../../ui/RouterLink";
import { useEditForm } from "../shared/conflict";
import {
  problemLines,
  unmatchedLines,
  useFieldFocus,
  type FieldMessages,
} from "../shared/formErrors";
import { adminIndexQuery, adminKeys, type IndexDetail, type IndexOptions } from "../shared/queries";
import { useAdminToast } from "../shared/toast";
import { ChangeWarning, IndexForm, canSendIndexForm } from "./IndexForm";
import {
  checkIndexForm,
  indexFieldOrder,
  indexFormValues,
  indexSchemaFields,
  methodOf,
  routeIndexErrors,
  schemaOf,
  settingsChanged,
  updateIndexBody,
} from "./indexForm";

const useStyles = makeStyles({
  actions: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
  buttons: { display: "flex", flexWrap: "wrap", columnGap: tokens.spacingHorizontalS },
});

interface Failure {
  readonly error: ApiError;
  readonly lines: readonly string[];
  readonly backToList?: boolean;
}

export interface EditIndexSettingsProps {
  readonly index: IndexDetail;
  readonly options: IndexOptions;
  /** Cancel, or a save that succeeded. */
  readonly onClose: () => void;
}

export function EditIndexSettings({ index, options, onClose }: EditIndexSettingsProps) {
  const styles = useStyles();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { register, focusFirst } = useFieldFocus();
  const form = useEditForm(indexFormValues(index), index.rowVersion);
  const [errors, setErrors] = useState<FieldMessages>(new Map());
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const indexId = index.indexId;
  const schema = schemaOf(methodOf(options, form.draft.method));
  const body = updateIndexBody(form.base, form.draft, schema);

  const showErrors = (messages: FieldMessages) => {
    setErrors(messages);
    focusFirst(indexFieldOrder(schema), messages);
  };

  /** Section 7.4 step 4: read the index once more; the draft stays. */
  const conflict = async (error: ApiError) => {
    setFailure({ error, lines: [en["admin.conflict.body"]] });
    try {
      const latest: IndexDetail = await queryClient.query({
        ...adminIndexQuery(indexId),
        staleTime: 0,
      });
      form.conflict(indexFormValues(latest), latest.rowVersion);
    } catch {
      // The draft stays; the next Save meets the same 412 and reads once more.
    }
  };

  const save = async () => {
    setFailure(null);
    const checked = checkIndexForm(form.draft, schema);
    const messages = checked.size > 0 ? checked : indexSchemaFields(body, false);
    if (messages.size > 0) {
      showErrors(messages);
      return;
    }
    setErrors(new Map());
    setBusy(true);
    try {
      const updated = await request("adminUpdateIndex", {
        path: { indexId },
        body,
        ifMatch: form.rowVersion,
      });
      queryClient.setQueryData(adminKeys.index(indexId), updated);
      form.saved(indexFormValues(updated), updated.rowVersion);
      void queryClient.invalidateQueries({ queryKey: adminKeys.indexLists });
      toast(en["index.saved"]);
      onClose();
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return;
      waits.waitFor("save", error);
      if (error.status === 412) {
        await conflict(error);
      } else if (isProblem(error, 409, "display_name_taken")) {
        showErrors(new Map([["displayName", error.detail ?? ""]]));
      } else if (error.status === 422) {
        const routed = routeIndexErrors(error, schema);
        showErrors(routed.messages);
        if (routed.unmatched.length > 0 || routed.messages.size === 0) {
          setFailure({ error, lines: unmatchedLines(routed.unmatched) });
        }
      } else {
        setFailure({
          error,
          lines: problemLines(error),
          backToList: isProblem(error, 404, "index_not_found"),
        });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <IndexForm
      options={options}
      values={form.draft}
      onChange={(next) => {
        form.replaceDraft(next);
      }}
      errors={errors}
      conflicts={form.conflicts}
      onUseLatest={(name) => {
        form.takeLatest(name);
      }}
      register={register}
      {...(settingsChanged(form.base, form.draft) ? { warning: <ChangeWarning /> } : {})}
      actions={
        <div className={styles.actions}>
          {failure === null ? null : (
            <ApiErrorBar
              error={failure.error}
              lines={failure.lines}
              {...(failure.backToList === true
                ? {
                    actions: (
                      <RouterLink to="/admin/indexes">
                        {fillText(en["admin.backToList"], { list: en["indexes.title"] })}
                      </RouterLink>
                    ),
                  }
                : {})}
            />
          )}
          <div className={styles.buttons}>
            <Button
              appearance="primary"
              disabled={
                busy ||
                Object.keys(body).length === 0 ||
                !canSendIndexForm(options, form.draft) ||
                waits.isWaiting("save")
              }
              {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={() => {
                void save();
              }}
            >
              {en["index.save"]}
            </Button>
            <Button onClick={onClose} disabled={busy}>
              {en["confirm.cancel"]}
            </Button>
          </div>
        </div>
      }
    />
  );
}
