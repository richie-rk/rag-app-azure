import {
  makeStyles,
  tokens,
  Text,
  Link,
  Divider,
} from "@fluentui/react-components";
import { DocumentRegular } from "@fluentui/react-icons";

const useStyles = makeStyles({
  panel: {
    width: "320px",
    borderLeft: `1px solid ${tokens.colorNeutralStroke1}`,
    padding: "16px",
    overflowY: "auto",
    backgroundColor: tokens.colorNeutralBackground2,
  },
  item: {
    display: "flex",
    alignItems: "flex-start",
    gap: "8px",
    padding: "8px 0",
  },
  content: {
    fontSize: tokens.fontSizeBase200,
    color: tokens.colorNeutralForeground2,
    marginTop: "4px",
  },
});

interface Props {
  dataPoints: string[];
  visible: boolean;
}

export function CitationPanel({ dataPoints, visible }: Props) {
  const styles = useStyles();

  if (!visible || !dataPoints.length) return null;

  return (
    <div className={styles.panel}>
      <Text weight="semibold" size={400}>
        Sources
      </Text>
      <Divider style={{ margin: "8px 0" }} />
      {dataPoints.map((dp, i) => {
        const [source, ...contentParts] = dp.split(":");
        const content = contentParts.join(":").trim();
        return (
          <div key={i} className={styles.item}>
            <DocumentRegular />
            <div>
              <Link>{source}</Link>
              {content && (
                <div className={styles.content}>
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
  );
}
