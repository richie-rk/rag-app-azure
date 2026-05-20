import { Configuration, PublicClientApplication } from "@azure/msal-browser";

const msalConfig: Configuration = {
  auth: {
    clientId: import.meta.env.VITE_MSAL_CLIENT_ID || "",
    authority: `https://login.microsoftonline.com/${import.meta.env.VITE_MSAL_TENANT_ID || "common"}`,
    redirectUri: import.meta.env.VITE_MSAL_REDIRECT_URI || window.location.origin,
  },
  cache: {
    cacheLocation: "localStorage",
    storeAuthStateInCookie: false,
  },
};

export const msalInstance = new PublicClientApplication(msalConfig);

export const loginRequest = {
  scopes: ["User.Read", "GroupMember.Read.All"],
};

// Scopes for the backend API. VITE_API_SCOPE must be the URI of an exposed
// scope on the App Registration, for example `api://<client-id>/access_as_user`.
// The token returned for this request is what backends validate via JWKS. See
// ADR-0003.
export const apiRequest = {
  scopes: [import.meta.env.VITE_API_SCOPE || ""],
};
