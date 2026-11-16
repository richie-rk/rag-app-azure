// New index (`/admin/indexes/new`, 10-web-client.md section 7.7): loads `adminGetIndexOptions`,
// starts every field from `defaults`, and Create index calls `adminCreateIndex`; its `202` opens
// the new index's page with the toast `index.created`.

import { Button, Spinner, makeStyles, tokens } from "@fluentui/react-components";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { request } from "../../api/http";
import { apiErrorOf, isProblem, type ApiError } from "../../api/problem";
import { en } from "../../copy/en";
import { ApiErrorBar } from "../../ui/ErrorBar";
import { PageHeader, useDocumentTitle } from "../../ui/PageHeader";
import { useRetryWaits } from "../../ui/retryWait";
import { LoadErrorBar } from "../shared/AdminStates";
import {
  problemLines,
  unmatchedLines,
  useFieldFocus,
  type FieldMessages,
} from "../shared/formErrors";
import { adminKeys, indexOptionsQuery, type IndexOptions } from "../shared/queries";
import { useAdminToast } from "../shared/toast";
import { IndexForm, canSendIndexForm } from "./IndexForm";
import {
  checkIndexForm,
  createIndexBody,
  defaultIndexValues,
  indexFieldOrder,
  indexSchemaFields,
  methodOf,
  routeIndexErrors,
  schemaOf,
  type IndexFormValues,
} from "./indexForm";

const useStyles = makeStyles({
  root: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalM },
  actions: { display: "flex", flexDirection: "column", rowGap: tokens.spacingVerticalS },
});

export function NewIndexScreen() {
  const styles = useStyles();
  useDocumentTitle(en["index.new.title"]);
  const options = useQuery(indexOptionsQuery());
  const error = options.isError ? apiErrorOf(options.error) : null;
  return (
    <div className={styles.root}>
      <PageHeader
        title={en["index.new.title"]}
        breadcrumbs={[
          { label: en["indexes.title"], to: "/admin/indexes" },
          { label: en["index.new.title"] },
        ]}
      />
      {options.data !== undefined ? (
        <NewIndexForm options={options.data} />
      ) : error !== null ? (
        <LoadErrorBar
          title={en["index.optionsError"]}
          error={error}
          onRetry={() => {
            void options.refetch();
          }}
        />
      ) : (
        <Spinner size="small" aria-label={en["index.new.title"]} />
      )}
    </div>
  );
}

function NewIndexForm({ options }: { readonly options: IndexOptions }) {
  const styles = useStyles();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useAdminToast();
  const waits = useRetryWaits();
  const { register, focusFirst } = useFieldFocus();
  const [values, setValues] = useState<IndexFormValues>(() => defaultIndexValues(options));
  const [errors, setErrors] = useState<FieldMessages>(new Map());
  const [failure, setFailure] = useState<{ error: ApiError; lines: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const schema = schemaOf(methodOf(options, values.method));

  const showErrors = (messages: FieldMessages) => {
    setErrors(messages);
    focusFirst(indexFieldOrder(schema), messages);
  };

  const create = async () => {
    setFailure(null);
    const body = createIndexBody(values, schema);
    const checked = checkIndexForm(values, schema);
    const messages = checked.size > 0 ? checked : indexSchemaFields(body, true);
    if (messages.size > 0) {
      showErrors(messages);
      return;
    }
    setErrors(new Map());
    setBusy(true);
    try {
      const started = await request("adminCreateIndex", { body });
      queryClient.setQueryData(adminKeys.index(started.index.indexId), started.index);
      void queryClient.invalidateQueries({ queryKey: adminKeys.indexLists });
      void navigate(`/admin/indexes/${started.index.indexId}`);
      toast(en["index.created"]);
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (error === null || error.kind === "aborted") return;
      waits.waitFor("create", error);
      if (isProblem(error, 409, "display_name_taken")) {
        showErrors(new Map([["displayName", error.detail ?? ""]]));
      } else if (error.status === 422) {
        const routed = routeIndexErrors(error, schema);
        showErrors(routed.messages);
        if (routed.unmatched.length > 0 || routed.messages.size === 0) {
          setFailure({ error, lines: unmatchedLines(routed.unmatched) });
        }
      } else {
        setFailure({ error, lines: problemLines(error) });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <IndexForm
      options={options}
      values={values}
      onChange={setValues}
      errors={errors}
      register={register}
      actions={
        <div className={styles.actions}>
          {failure === null ? null : <ApiErrorBar error={failure.error} lines={failure.lines} />}
          <div>
            <Button
              appearance="primary"
              disabled={busy || !canSendIndexForm(options, values) || waits.isWaiting("create")}
              {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
              onClick={() => {
                void create();
              }}
            >
              {en["index.create"]}
            </Button>
          </div>
        </div>
      }
    />
  );
}
