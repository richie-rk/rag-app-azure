import { createContext, useContext, type ReactNode } from "react";
import { FluentProvider } from "@fluentui/react-components";
import { useTheme, type ThemeMode } from "../hooks/useTheme";

export type { ThemeMode } from "../hooks/useTheme";

interface ThemeContextValue {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  mode: "system",
  setMode: () => {},
});

export function useThemeContext() {
  return useContext(ThemeContext);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const { theme, mode, setMode } = useTheme();

  return (
    <ThemeContext.Provider value={{ mode, setMode }}>
      <FluentProvider theme={theme}>
        {children}
      </FluentProvider>
    </ThemeContext.Provider>
  );
}
