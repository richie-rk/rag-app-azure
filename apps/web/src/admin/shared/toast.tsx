// Success on the admin screens (10-web-client.md section 7.1): a polite Fluent Toast shown for
// 6,000 ms, paused while hovered or focused. Errors never go here: they are inline error bars,
// because a toast can vanish before it is read. The toaster sits in the admin layout, so a toast
// survives the navigation that follows a create or a delete.

import {
  Toast,
  ToastFooter,
  ToastTitle,
  Toaster,
  useToastController,
} from "@fluentui/react-components";
import { useCallback } from "react";
import type { To } from "react-router";

import { announceStatus } from "../../a11y/announce";
import { TOAST_MS } from "../../platform/constants";
import { RouterLink } from "../../ui/RouterLink";

export const ADMIN_TOASTER_ID = "docchat-admin-toaster";

/** The admin screens' toaster. Its announcements go through the app's polite status region
 * (section 11.2), the only live region the client writes to. */
export function AdminToaster() {
  return (
    <Toaster
      toasterId={ADMIN_TOASTER_ID}
      position="bottom-end"
      pauseOnHover
      timeout={TOAST_MS}
      announce={(message) => {
        announceStatus(message);
      }}
    />
  );
}

/** A link in a toast, such as `index.viewProgress` to the run a re-ingest started. */
export interface ToastLink {
  readonly label: string;
  readonly to: To;
}

export type ShowToast = (text: string, link?: ToastLink) => void;

let toastCount = 0;

/** Shows a success toast on the admin toaster. */
export function useAdminToast(): ShowToast {
  const { dispatchToast, pauseToast, playToast } = useToastController(ADMIN_TOASTER_ID);
  return useCallback(
    (text: string, link?: ToastLink) => {
      toastCount += 1;
      const toastId = `admin-toast-${String(toastCount)}`;
      dispatchToast(
        <Toast>
          <ToastTitle>{text}</ToastTitle>
          {link === undefined ? null : (
            <ToastFooter>
              <RouterLink to={link.to}>{link.label}</RouterLink>
            </ToastFooter>
          )}
        </Toast>,
        {
          toastId,
          intent: "success",
          politeness: "polite",
          timeout: TOAST_MS,
          pauseOnHover: true,
          // The toast's container takes these as its own: focus inside pauses the timer, as
          // hovering does, and leaving it resumes.
          data: {
            root: {
              onFocus: () => {
                pauseToast(toastId);
              },
              onBlur: () => {
                playToast(toastId);
              },
            },
          },
        },
      );
    },
    [dispatchToast, pauseToast, playToast],
  );
}
