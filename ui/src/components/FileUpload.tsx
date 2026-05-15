import { useRef, useState, useCallback } from "react";
import { Button, Text, tokens, makeStyles } from "@fluentui/react-components";
import { ArrowUploadRegular, DocumentRegular, DismissRegular } from "@fluentui/react-icons";

const useStyles = makeStyles({
  dropzone: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    gap: "8px",
    padding: "32px 16px",
    height: "160px",
    boxSizing: "border-box",
    border: `2px dashed ${tokens.colorNeutralStroke1}`,
    borderRadius: "12px",
    backgroundColor: tokens.colorNeutralBackground2,
    cursor: "pointer",
    transitionProperty: "border-color, background-color",
    transitionDuration: "0.2s",
    "&:hover": {
      backgroundColor: tokens.colorNeutralBackground3,
    },
  },
  dropzoneActive: {
    backgroundColor: tokens.colorNeutralBackground3,
  },
  uploadIcon: {
    fontSize: "48px",
    color: tokens.colorNeutralForeground3,
  },
  mainText: {
    fontSize: "14px",
    color: tokens.colorNeutralForeground2,
  },
  subText: {
    fontSize: "12px",
    color: tokens.colorNeutralForeground4,
  },
  fileList: {
    display: "flex",
    flexDirection: "column",
    gap: "6px",
    marginTop: "12px",
  },
  fileItem: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "8px 12px",
    borderRadius: "6px",
    backgroundColor: tokens.colorNeutralBackground3,
  },
  fileIcon: {
    color: tokens.colorBrandForeground1,
    fontSize: "16px",
  },
  fileName: {
    flex: 1,
    fontSize: "13px",
    color: tokens.colorNeutralForeground1,
  },
  fileSize: {
    fontSize: "12px",
    color: tokens.colorNeutralForeground3,
  },
  uploadActions: {
    display: "flex",
    justifyContent: "flex-end",
    marginTop: "12px",
  },
});

interface Props {
  onUpload: (files: File[]) => void;
  accept?: string;
  multiple?: boolean;
}

export function FileUpload({ onUpload, accept, multiple = true }: Props) {
  const styles = useStyles();
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      setFiles(Array.from(e.target.files));
    }
  };

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) {
      setFiles(Array.from(e.dataTransfer.files));
    }
  }, []);

  const handleUpload = () => {
    if (files.length > 0) {
      onUpload(files);
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <>
      <div
        className={`${styles.dropzone} ${dragging ? styles.dropzoneActive : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <ArrowUploadRegular className={styles.uploadIcon} />
        <Text className={styles.mainText}>Drag & drop files here</Text>
        <Button appearance="outline" size="small" onClick={(e) => { e.stopPropagation(); inputRef.current?.click(); }}>
          Browse Files
        </Button>
        <Text className={styles.subText}>
          Supported: PDF, DOCX, PPTX, TXT, MD
        </Text>
        <input
          ref={inputRef}
          type="file"
          accept={accept || ".pdf,.docx,.pptx,.txt,.md"}
          multiple={multiple}
          onChange={handleChange}
          style={{ display: "none" }}
        />
      </div>

      {files.length > 0 && (
        <>
          <div className={styles.fileList}>
            {files.map((f, i) => (
              <div key={i} className={styles.fileItem}>
                <DocumentRegular className={styles.fileIcon} />
                <span className={styles.fileName}>{f.name}</span>
                <span className={styles.fileSize}>
                  {(f.size / 1024).toFixed(1)} KB
                </span>
                <Button
                  appearance="subtle"
                  icon={<DismissRegular />}
                  size="small"
                  onClick={() => removeFile(i)}
                />
              </div>
            ))}
          </div>
          <div className={styles.uploadActions}>
            <Button appearance="primary" onClick={handleUpload}>
              Upload {files.length} file{files.length > 1 ? "s" : ""}
            </Button>
          </div>
        </>
      )}
    </>
  );
}
