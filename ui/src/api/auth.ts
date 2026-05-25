/**
 * Auth-related API calls. The admin invite UI in UsersPage uses createMagicLink;
 * the magic-link verify flow during sign-in lives in LoginPage.
 */

import { apiClient } from "./client";

export interface MagicLinkResponse {
  link: string;
  message: string;
}

export async function createMagicLink(
  email: string,
  token?: string,
): Promise<MagicLinkResponse> {
  return apiClient("/auth/magic-link", {
    method: "POST",
    body: JSON.stringify({ email }),
    token,
  });
}
