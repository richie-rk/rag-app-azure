// The top bar (10-web-client.md section 6.3): a 48 px `header` with the navigation toggle and the
// product mark on the left, the theme toggle and the account menu on the right.

import {
  Avatar,
  Button,
  Hamburger,
  Menu,
  MenuDivider,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Persona,
  Text,
  ToggleButton,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import {
  DocumentSearch24Regular,
  WeatherMoon20Regular,
  WeatherSunny20Regular,
} from "@fluentui/react-icons";

import { en } from "../copy/en";
import { BREAKPOINT_MEDIUM_PX } from "../platform/constants";
import { fillText } from "../ui/format";
import { RouterLink } from "../ui/RouterLink";
import { setTheme, useThemeChoice } from "./theme";

const useStyles = makeStyles({
  header: {
    boxSizing: "border-box",
    height: "48px",
    flexShrink: 0,
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    columnGap: tokens.spacingHorizontalS,
    paddingLeft: tokens.spacingHorizontalS,
    paddingRight: tokens.spacingHorizontalS,
    borderBottom: `${tokens.strokeWidthThin} solid ${tokens.colorNeutralStroke2}`,
    backgroundColor: tokens.colorNeutralBackground1,
  },
  side: { display: "flex", alignItems: "center", columnGap: tokens.spacingHorizontalS },
  mark: {
    display: "inline-flex",
    alignItems: "center",
    columnGap: tokens.spacingHorizontalXS,
    color: tokens.colorNeutralForeground1,
    fontWeight: tokens.fontWeightSemibold,
  },
  accountName: {
    [`@media (max-width: ${String(BREAKPOINT_MEDIUM_PX - 1)}px)`]: { display: "none" },
  },
});

/** Up to two initials of the display name, for the avatar. */
export function initials(displayName: string): string {
  return displayName
    .split(/\s+/)
    .filter((word) => word.length > 0)
    .slice(0, 2)
    .map((word) =>
      String.fromCodePoint(word.codePointAt(0) ?? 0x20)
        .trim()
        .toUpperCase(),
    )
    .join("");
}

export interface TopBarProps {
  readonly navOpen: boolean;
  readonly onToggleNav: () => void;
  readonly displayName: string;
  readonly email: string;
  readonly onSignOut: () => void;
}

export function TopBar({ navOpen, onToggleNav, displayName, email, onSignOut }: TopBarProps) {
  const styles = useStyles();
  const theme = useThemeChoice();
  const dark = theme === "dark";
  return (
    <header className={styles.header}>
      <div className={styles.side}>
        <Hamburger
          aria-label={navOpen ? en["shell.nav.hide"] : en["shell.nav.show"]}
          aria-expanded={navOpen}
          onClick={onToggleNav}
        />
        <RouterLink to="/chat" className={styles.mark}>
          <DocumentSearch24Regular aria-hidden="true" />
          <span>{en["shell.productName"]}</span>
        </RouterLink>
      </div>
      <div className={styles.side}>
        <ToggleButton
          appearance="subtle"
          checked={dark}
          aria-label={en["shell.theme"]}
          icon={dark ? <WeatherSunny20Regular /> : <WeatherMoon20Regular />}
          onClick={() => {
            setTheme(dark ? "light" : "dark");
          }}
        />
        <Menu>
          <MenuTrigger disableButtonEnhancement>
            <Button
              appearance="subtle"
              aria-label={fillText(en["shell.account.label"], { displayName })}
              icon={
                <Avatar
                  name={displayName}
                  initials={initials(displayName)}
                  size={24}
                  aria-hidden="true"
                />
              }
            >
              <Text className={styles.accountName}>{displayName}</Text>
            </Button>
          </MenuTrigger>
          <MenuPopover>
            <MenuList>
              <Persona
                name={displayName}
                secondaryText={email}
                avatar={{ initials: initials(displayName) }}
              />
              <MenuDivider />
              <MenuItem onClick={onSignOut}>{en["shell.account.signOut"]}</MenuItem>
            </MenuList>
          </MenuPopover>
        </Menu>
      </div>
    </header>
  );
}
