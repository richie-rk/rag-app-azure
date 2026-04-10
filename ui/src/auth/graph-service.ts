import { Client } from "@microsoft/microsoft-graph-client";

function getGraphClient(accessToken: string): Client {
  return Client.init({
    authProvider: (callback) => callback(null, accessToken),
  });
}

export interface UserProfile {
  displayName: string;
  mail: string;
  id: string;
}

export async function fetchUserProfile(accessToken: string): Promise<UserProfile> {
  const client = getGraphClient(accessToken);
  const user = await client.api("/me").get();
  return {
    displayName: user.displayName || "",
    mail: user.mail || user.userPrincipalName || "",
    id: user.id || "",
  };
}

export async function fetchUserGroups(accessToken: string): Promise<string[]> {
  const client = getGraphClient(accessToken);
  const response = await client.api("/me/memberOf").get();
  return (response.value || []).map((g: { id: string }) => g.id);
}
