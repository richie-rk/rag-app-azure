// The Session expired bar (10-web-client.md section 4.5 step 4): above the main content of every
// screen while the session-expired state is set. The client never redirects on its own once the
// shell is on screen, so the user can copy unsent text first.

import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
} from "@fluentui/react-components";

import { en } from "../copy/en";
import { signInAgain, useSessionExpired } from "./tokens";

export function SessionExpiredBar() {
  const expired = useSessionExpired();
  if (!expired) return null;
  return (
    <MessageBar intent="warning" layout="multiline">
      <MessageBarBody>
        <MessageBarTitle>{en["session.expired.title"]}</MessageBarTitle>
        {en["session.expired.body"]}
      </MessageBarBody>
      <MessageBarActions>
        <Button
          appearance="primary"
          onClick={() => {
            void signInAgain().catch(() => undefined);
          }}
        >
          {en["session.expired.action"]}
        </Button>
      </MessageBarActions>
    </MessageBar>
  );
}
