// The access states (10-web-client.md sections 4.8 and 4.9). The full-page states replace the
// whole page with a centred column; No projects and Admin only appear inside the app shell.

import { Button, Spinner, Text, makeStyles, tokens } from "@fluentui/react-components";
import { DocumentSearch32Regular } from "@fluentui/react-icons";
import { useId, type ReactNode } from "react";
import { useNavigate } from "react-router";

import type { ApiError } from "../api/problem";
import { en } from "../copy/en";
import { ApiErrorBar, ReferenceIdLine } from "../ui/ErrorBar";
import { fillText } from "../ui/format";
import { ScreenHeading, useDocumentTitle } from "../ui/PageHeader";

const useStyles = makeStyles({
  page: {
    boxSizing: "border-box",
    minHeight: "100vh",
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    padding: tokens.spacingHorizontalL,
    backgroundColor: tokens.colorNeutralBackground1,
    color: tokens.colorNeutralForeground1,
  },
  column: {
    width: "100%",
    maxWidth: "480px",
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalL,
  },
  mark: {
    display: "flex",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalS,
    color: tokens.colorBrandForeground1,
  },
  heading: {
    margin: "0",
    fontSize: tokens.fontSizeBase600,
    lineHeight: tokens.lineHeightBase600,
    fontWeight: tokens.fontWeightSemibold,
  },
  actions: { display: "flex", flexWrap: "wrap", gap: tokens.spacingHorizontalS },
  inShell: {
    display: "flex",
    flexDirection: "column",
    rowGap: tokens.spacingVerticalM,
    maxWidth: "480px",
  },
  subheading: {
    margin: "0",
    fontSize: tokens.fontSizeBase500,
    lineHeight: tokens.lineHeightBase500,
    fontWeight: tokens.fontWeightSemibold,
  },
});

function ProductMark() {
  const styles = useStyles();
  return (
    <div className={styles.mark}>
      <DocumentSearch32Regular aria-hidden="true" />
      <Text weight="semibold" size={500}>
        {en["shell.productName"]}
      </Text>
    </div>
  );
}

interface FullPageStateProps {
  /** The `h1`; null for Starting and Redirecting, which show only their text. */
  readonly title: string | null;
  readonly children?: ReactNode;
  readonly actions?: ReactNode;
  readonly referenceId?: string | null;
}

/** The centred column of every full-page state: the product mark, the `h1`, the body, the actions
 * (the primary one first) and, where listed, the reference ID line. */
function FullPageState({ title, children, actions, referenceId }: FullPageStateProps) {
  const styles = useStyles();
  useDocumentTitle(...(title === null ? [] : [title]));
  return (
    <div className={styles.page}>
      <main className={styles.column}>
        <ProductMark />
        {title === null ? null : <h1 className={styles.heading}>{title}</h1>}
        {children}
        {actions === undefined ? null : <div className={styles.actions}>{actions}</div>}
        {referenceId === undefined || referenceId === null ? null : (
          <ReferenceIdLine referenceId={referenceId} />
        )}
      </main>
    </div>
  );
}

export function StartingScreen() {
  return (
    <FullPageState title={null}>
      <Spinner label={en["app.starting"]} labelPosition="after" />
    </FullPageState>
  );
}

export function RedirectingScreen() {
  return (
    <FullPageState title={null}>
      <Text>{en["app.redirecting"]}</Text>
    </FullPageState>
  );
}

export function UnsupportedBrowserScreen() {
  return (
    <FullPageState title={en["access.unsupported.title"]}>
      <Text>{en["access.unsupported.body"]}</Text>
    </FullPageState>
  );
}

export function ConfigErrorScreen({ onReload }: { readonly onReload: () => void }) {
  return (
    <FullPageState
      title={en["app.configError.title"]}
      actions={
        <Button appearance="primary" onClick={onReload}>
          {en["app.reload"]}
        </Button>
      }
    >
      <Text>{en["app.configError.body"]}</Text>
    </FullPageState>
  );
}

export function SignedOutScreen({ onSignIn }: { readonly onSignIn: () => void }) {
  return (
    <FullPageState
      title={en["access.signedOut.title"]}
      actions={
        <Button appearance="primary" onClick={onSignIn}>
          {en["access.signedOut.action"]}
        </Button>
      }
    >
      <Text>{en["access.signedOut.body"]}</Text>
    </FullPageState>
  );
}

export interface NotAssignedScreenProps {
  /** MSAL's `account.username`, so the user can tell an administrator which account to assign. */
  readonly account: string;
  readonly onRetry: () => void;
  readonly onSignOut: () => void;
}

export function NotAssignedScreen({ account, onRetry, onSignOut }: NotAssignedScreenProps) {
  return (
    <FullPageState
      title={en["access.notAssigned.title"]}
      actions={
        <>
          <Button appearance="primary" onClick={onRetry}>
            {en["access.retry"]}
          </Button>
          <Button onClick={onSignOut}>{en["access.signOut"]}</Button>
        </>
      }
    >
      <Text>{fillText(en["access.notAssigned.body"], { account })}</Text>
    </FullPageState>
  );
}

export interface ConsentMissingScreenProps {
  readonly referenceId: string | null;
  readonly onSignOut: () => void;
}

export function ConsentMissingScreen({ referenceId, onSignOut }: ConsentMissingScreenProps) {
  return (
    <FullPageState
      title={en["access.consentMissing.title"]}
      referenceId={referenceId}
      actions={
        <Button appearance="primary" onClick={onSignOut}>
          {en["access.signOut"]}
        </Button>
      }
    >
      <Text>{en["access.consentMissing.body"]}</Text>
    </FullPageState>
  );
}

export interface SignInFailedScreenProps {
  /** The AuthError's `correlationId`; none when the MSAL instance couldn't be created. */
  readonly referenceId: string | null;
  readonly onRetry: () => void;
}

export function SignInFailedScreen({ referenceId, onRetry }: SignInFailedScreenProps) {
  return (
    <FullPageState
      title={en["access.signInFailed.title"]}
      referenceId={referenceId}
      actions={
        <Button appearance="primary" onClick={onRetry}>
          {en["access.retry"]}
        </Button>
      }
    >
      <Text>{en["access.signInFailed.body"]}</Text>
    </FullPageState>
  );
}

export interface LoadUserFailedScreenProps {
  /** The failed `getCurrentUser` call, shown in its error bar with its reference ID. */
  readonly error: ApiError;
  readonly onRetry: () => void;
  readonly onSignOut: () => void;
}

export function LoadUserFailedScreen({ error, onRetry, onSignOut }: LoadUserFailedScreenProps) {
  return (
    <FullPageState
      title={en["access.loadUserFailed.title"]}
      actions={
        <>
          <Button appearance="primary" onClick={onRetry}>
            {en["access.retry"]}
          </Button>
          <Button onClick={onSignOut}>{en["access.signOut"]}</Button>
        </>
      }
    >
      <Text>{en["access.loadUserFailed.body"]}</Text>
      <ApiErrorBar error={error} />
    </FullPageState>
  );
}

export interface NoProjectsStateProps {
  readonly isAdmin: boolean;
  /** Refetches `listMyProjects`. */
  readonly onRefresh: () => void;
}

/** The chat screen's main panel when the first page of `listMyProjects` has no items (section
 * 4.7): a user asks an administrator and can refresh; an admin is sent to Projects. */
export function NoProjectsState({ isAdmin, onRefresh }: NoProjectsStateProps) {
  const styles = useStyles();
  const navigate = useNavigate();
  const titleId = useId();
  return (
    <section className={styles.inShell} aria-labelledby={titleId}>
      <h2 id={titleId} className={styles.subheading}>
        {isAdmin ? en["access.noProjectsAdmin.title"] : en["access.noProjects.title"]}
      </h2>
      <Text>{isAdmin ? en["access.noProjectsAdmin.body"] : en["access.noProjects.body"]}</Text>
      <div className={styles.actions}>
        {isAdmin ? (
          <Button appearance="primary" onClick={() => void navigate("/admin/projects")}>
            {en["access.noProjectsAdmin.action"]}
          </Button>
        ) : (
          <Button appearance="primary" onClick={onRefresh}>
            {en["access.noProjects.action"]}
          </Button>
        )}
      </div>
    </section>
  );
}

/** The main content of an admin route while `isAdmin` is false (section 4.7). */
export function AdminOnlyScreen() {
  const styles = useStyles();
  const navigate = useNavigate();
  useDocumentTitle(en["access.adminOnly.title"]);
  return (
    <div className={styles.inShell}>
      <ScreenHeading className={styles.heading}>{en["access.adminOnly.title"]}</ScreenHeading>
      <Text>{en["access.adminOnly.body"]}</Text>
      <div className={styles.actions}>
        <Button appearance="primary" onClick={() => void navigate("/chat")}>
          {en["access.adminOnly.action"]}
        </Button>
      </div>
    </div>
  );
}
