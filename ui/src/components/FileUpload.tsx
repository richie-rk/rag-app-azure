import { useRef, useState } from "react";
import { Button, Text, tokens, makeStyles } from "@fluentui/react-components";
import { ArrowUploadRegular } from "@fluentui/react-icons";

const useStyles = makeStyles({
  container: {
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    padding: "16px",
    border: `2px dashed ${tokens.colorNeutralStroke1}`,
    borderRadius: "8px",
    alignItems: "center",
  },
  fileList: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
    width: "100%",
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

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      const selected = Array.from(e.target.files);
      setFiles(selected);
    }
  };

  const handleUpload = () => {
    if (files.length > 0) {
      onUpload(files);
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <div className={styles.container}>
      <ArrowUploadRegular style={{ fontSize: 32 }} />
      <Text>Select files to upload</Text>
      <input
        ref={inputRef}
        type="file"
        accept={accept || ".pdf,.docx,.pptx,.txt,.md"}
        multiple={multiple}
        onChange={handleChange}
        style={{ display: "none" }}
      />
      <Button onClick={() => inputRef.current?.click()}>Browse Files</Button>
      {files.length > 0 && (
        <div className={styles.fileList}>
          {files.map((f, i) => (
            <Text key={i} size={200}>
              {f.name} ({(f.size / 1024).toFixed(1)} KB)
            </Text>
          ))}
          <Button appearance="primary" onClick={handleUpload}>
            Upload {files.length} file{files.length > 1 ? "s" : ""}
          </Button>
        </div>
      )}
    </div>
  );
}
