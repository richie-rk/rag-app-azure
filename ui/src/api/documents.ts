/**
 * Document fetch for citation viewing.
 *
 * get_document requires the bearer token, so the bytes are pulled through the
 * authenticated API client; a plain link or window.open(url) would 401.
 */

import { apiClient } from "./client";

export async function fetchDocumentBlob(
  fileName: string,
  token: string,
): Promise<Blob> {
  const response: Response = await apiClient(
    `/documents?file_name=${encodeURIComponent(fileName)}`,
    { token },
  );
  return response.blob();
}
