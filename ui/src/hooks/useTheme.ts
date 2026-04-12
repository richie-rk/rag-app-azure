import { useState, useEffect, useCallback } from "react";
import { webLightTheme, webDarkTheme, type Theme } from "@fluentui/react-components";
import { lightTheme, darkTheme } from "../styles/theme";

export type ThemeMode = "light" | "dark" | "system";

function getSystemPreference(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(mode: ThemeMode): Theme {
  const effective = mode === "system" ? getSystemPreference() : mode;
  return effective === "dark" ? { ...webDarkTheme, ...darkTheme } : { ...webLightTheme, ...lightTheme };
}

export function useTheme() {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    return (localStorage.getItem("rag_theme") as ThemeMode) || "system";
  });

  const [theme, setTheme] = useState<Theme>(() => resolveTheme(mode));

  const setMode = useCallback((newMode: ThemeMode) => {
    setModeState(newMode);
    localStorage.setItem("rag_theme", newMode);
  }, []);

  useEffect(() => {
    setTheme(resolveTheme(mode));

    if (mode === "system") {
      const mql = window.matchMedia("(prefers-color-scheme: dark)");
      const handler = () => setTheme(resolveTheme("system"));
      mql.addEventListener("change", handler);
      return () => mql.removeEventListener("change", handler);
    }
  }, [mode]);

  // Set document background for theme
  useEffect(() => {
    const isDark = mode === "dark" || (mode === "system" && getSystemPreference() === "dark");
    document.documentElement.style.colorScheme = isDark ? "dark" : "light";
  }, [mode, theme]);

  return { theme, mode, setMode };
}
