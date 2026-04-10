import { createLightTheme, createDarkTheme, type BrandVariants } from "@fluentui/react-components";

const brandColors: BrandVariants = {
  10: "#020305",
  20: "#111723",
  30: "#16263D",
  40: "#193253",
  50: "#1B3F6A",
  60: "#1B4C82",
  70: "#18599B",
  80: "#1267B4",
  90: "#3174C2",
  100: "#4F82C8",
  110: "#6790CF",
  120: "#7D9ED5",
  130: "#92ACDC",
  140: "#A6BBE2",
  150: "#BAC9E9",
  160: "#CDD8EF",
};

export const lightTheme = createLightTheme(brandColors);
export const darkTheme = createDarkTheme(brandColors);
