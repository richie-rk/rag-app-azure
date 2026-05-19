import {
  makeStyles,
  tokens,
  Text,
  Link,
  Divider,
} from "@fluentui/react-components";
import { DocumentRegular } from "@fluentui/react-icons";
import { parseSourcePage } from "../utils/markdown";

const useStyles = makeStyles({
  panel: {
    width: "320px",
    borderLeft: `1px solid ${tokens.colorNeutralStroke2}`,
    padding: "16px",
    overflowY: "auto",
    backgroundColor: tokens.colorNeutralBackground2,
    flexShrink: 0,
    display: "flex",
    flexDirection: "column",
  },
  header: {
    fontSize: "14px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorNeutralForeground1,
    marginBottom: "4px",
  },
  divider: {
    margin: "8px 0 12px 0",
  },
  list: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
  },
  item: {
    display: "flex",
    alignItems: "flex-start",
    gap: "10px",
    padding: "10px 8px",
    borderRadius: "6px",
    transitionProperty: "background-color",
    transitionDuration: "0.15s",
    "&:hover": {
      backgroundColor: tokens.colorNeutralBackground3Hover,
    },
  },
  itemIcon: {
    fontSize: "18px",
    color: tokens.colorBrandForeground1,
    flexShrink: 0,
    marginTop: "2px",
  },
  itemBody: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
    minWidth: 0,
  },
  sourceName: {
    fontSize: "13px",
    fontWeight: tokens.fontWeightSemibold,
    color: tokens.colorBrandForeground1,
    cursor: "pointer",
    "&:hover": {
      textDecorationLine: "underline",
    },
  },
  pageLabel: {
    fontSize: "11px",
    color: tokens.colorNeutralForeground3,
  },
  preview: {
    fontSize: "12px",
    color: tokens.colorNeutralForeground3,
    lineHeight: "1.4",
    display: "-webkit-box",
    WebkitLineClamp: 3,
    WebkitBoxOrient: "vertical",
    overflow: "hidden",
  },
});

interface Props {
  dataPoints: string[];
  visible: boolean;
  onCitationClick?: (source: string) => void;
}

export function CitationPanel({ dataPoints, visible, onCitationClick }: Props) {
  const styles = useStyles();

  if (!visible || !dataPoints.length) return null;

  return (
    <div className={styles.panel}>
      <Text className={styles.header}>Sources</Text>
      <Divider className={styles.divider} />
      <div className={styles.list}>
        {dataPoints.map((dp, i) => {
          const [source, ...contentParts] = dp.split(":");
          const content = contentParts.join(":").trim();
          const { sourcefile, page } = parseSourcePage(source);
          return (
            <div key={i} className={styles.item}>
              <DocumentRegular className={styles.itemIcon} />
              <div className={styles.itemBody}>
                <Link
                  className={styles.sourceName}
                  onClick={() => onCitationClick?.(source)}
                >
                  {sourcefile}
                </Link>
                {page != null && (
                  <Text className={styles.pageLabel}>Page {page}</Text>
                )}
                {content && (
                  <div className={styles.preview}>
                    {content.length > 200
                      ? content.substring(0, 200) + "..."
                      : content}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
