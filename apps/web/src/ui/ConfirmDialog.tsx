// The confirmation before every destructive action (10-web-client.md section 7.1): a modal
// Fluent Dialog naming the item and saying what happens, with the action button and Cancel. It
// opens with focus on its first control and returns focus to its trigger when it closes.

import {
  Button,
  Dialog,
  DialogActions,
  DialogBody,
  DialogContent,
  DialogSurface,
  DialogTitle,
  Spinner,
} from "@fluentui/react-components";

import { en } from "../copy/en";
import { FocusFirstControl } from "./FocusFirstControl";

export interface ConfirmDialogProps {
  readonly open: boolean;
  /** Names the item, such as `Delete {name}?` filled in. */
  readonly title: string;
  /** Says what happens. */
  readonly body: string;
  readonly actionLabel: string;
  /** `confirm.cancel` unless the dialog has its own, such as Stay signed in. */
  readonly cancelLabel?: string;
  readonly onConfirm: () => void;
  /** Cancel, Escape or closing the dialog. */
  readonly onCancel: () => void;
  /** While the action runs: the action button shows a tiny spinner and both buttons wait. */
  readonly busy?: boolean;
}

export function ConfirmDialog({
  open,
  title,
  body,
  actionLabel,
  cancelLabel = en["confirm.cancel"],
  onConfirm,
  onCancel,
  busy = false,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      modalType="modal"
      onOpenChange={(_, data) => {
        if (!data.open && !busy) onCancel();
      }}
    >
      <DialogSurface>
        <DialogBody>
          <DialogTitle>{title}</DialogTitle>
          <DialogContent>{body}</DialogContent>
          <DialogActions>
            <Button
              appearance="primary"
              onClick={onConfirm}
              disabled={busy}
              {...(busy ? { icon: <Spinner size="tiny" aria-hidden /> } : {})}
            >
              {actionLabel}
            </Button>
            <Button onClick={onCancel} disabled={busy}>
              {cancelLabel}
            </Button>
          </DialogActions>
          <FocusFirstControl />
        </DialogBody>
      </DialogSurface>
    </Dialog>
  );
}
