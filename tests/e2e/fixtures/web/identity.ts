// The stub identity provider (10-web-client.md section 13.2): `context.route` on
// `https://login.microsoftonline.com/**` answers MSAL's metadata and instance-discovery calls,
// `/authorize` with a `302` to the redirect URI carrying `code` and `state` (or the error a case
// needs), `/token` with an ID token carrying the authorize request's `nonce`, an access token, a
// refresh token and `client_info`, and `/logout` with a `302` to the post-logout URI. The site's
// bundle runs unchanged: MSAL, the CSP and every module see what Entra ID would send.

import { randomBytes } from "node:crypto";

import type { BrowserContext, Request, Route } from "@playwright/test";

import { IDS, USER_EMAIL, USER_NAME } from "./stub-api/builders.ts";

export const TENANT_ID = "11111111-1111-4111-8111-111111111111";
export const CLIENT_ID = "22222222-2222-4222-8222-222222222222";
export const API_SCOPE = `api://${TENANT_ID}/docchat-e2e-api/access_as_user`;
export const LOGIN_ORIGIN = "https://login.microsoftonline.com";

const ISSUER = `${LOGIN_ORIGIN}/${TENANT_ID}/v2.0`;
const AUTHORIZE_PATH = `/${TENANT_ID}/oauth2/v2.0/authorize`;
const TOKEN_PATH = `/${TENANT_ID}/oauth2/v2.0/token`;
const LOGOUT_PATH = `/${TENANT_ID}/oauth2/v2.0/logout`;

/** What one `/authorize` request answers. */
export type AuthorizeOutcome =
  | { readonly kind: "code" }
  /** An error redirect: `error`, `error_description` and the `error_uri` Entra ID adds. */
  | {
      readonly kind: "error";
      readonly error: string;
      readonly description: string;
      readonly errorNo?: string;
    }
  /** A code whose `state` returns with its user part replaced, as a forged response would. */
  | { readonly kind: "tamper"; readonly userState: string };

export interface IdentityOptions {
  /** Answers of consecutive `/authorize` requests; once used up, every request gets a code. */
  readonly authorize?: readonly AuthorizeOutcome[];
  /** False: token answers carry no refresh token, so renewal needs interaction. */
  readonly refreshToken?: boolean;
  /** True: every refresh-token grant answers `invalid_grant` with `AADSTS700084`, the expiry of a
   * single-page app's refresh token. */
  readonly refreshExpired?: boolean;
  /** The access token's lifetime; a list gives consecutive token answers theirs, its last
   * entry applying from then on. */
  readonly accessTokenLifetimeSeconds?: number | readonly number[];
}

export interface TokenRequest {
  readonly grantType: string;
  readonly params: URLSearchParams;
  readonly at: number;
}

function base64url(value: string | Buffer): string {
  return Buffer.from(value).toString("base64url");
}

function unsignedJwt(claims: Record<string, unknown>): string {
  const header = base64url(JSON.stringify({ typ: "JWT", alg: "RS256", kid: "stub" }));
  return `${header}.${base64url(JSON.stringify(claims))}.${base64url("stub-signature")}`;
}

function lifetimesOf(value: number | readonly number[] | undefined): number[] {
  if (value === undefined) return [3600];
  return typeof value === "number" ? [value] : [...value];
}

const CLIENT_INFO = base64url(JSON.stringify({ uid: IDS.user, utid: TENANT_ID }));

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

export class StubIdentityProvider {
  readonly authorizeRequests: URL[] = [];
  readonly tokenRequests: TokenRequest[] = [];
  readonly logoutRequests: URL[] = [];
  /** Requests to Entra ID that no stubbed endpoint answers: each one fails the test. */
  readonly unexpected: string[] = [];
  private readonly outcomes: AuthorizeOutcome[];
  private refreshToken: boolean;
  private refreshExpired: boolean;
  private lifetimes: number[];
  private readonly nonces = new Map<string, string>();
  private serial = 0;
  private readonly worldId: string;

  constructor(worldId: string, options: IdentityOptions = {}) {
    this.worldId = worldId;
    this.outcomes = [...(options.authorize ?? [])];
    this.refreshToken = options.refreshToken ?? true;
    this.refreshExpired = options.refreshExpired ?? false;
    this.lifetimes = lifetimesOf(options.accessTokenLifetimeSeconds);
  }

  /** Changes the options for requests from now on. */
  configure(options: IdentityOptions): void {
    if (options.authorize !== undefined) this.outcomes.push(...options.authorize);
    if (options.refreshToken !== undefined) this.refreshToken = options.refreshToken;
    if (options.refreshExpired !== undefined) this.refreshExpired = options.refreshExpired;
    if (options.accessTokenLifetimeSeconds !== undefined) {
      this.lifetimes = lifetimesOf(options.accessTokenLifetimeSeconds);
    }
  }

  /** The access tokens minted so far name the world, which is how the stub API finds it. */
  accessToken(serial: number): string {
    return `stub-at.${this.worldId}.${String(serial)}`;
  }

  async install(context: BrowserContext): Promise<void> {
    await context.route(`${LOGIN_ORIGIN}/**`, (route) => this.handle(route));
  }

  private async handle(route: Route): Promise<void> {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS });
      return;
    }
    if (url.pathname === `/${TENANT_ID}/v2.0/.well-known/openid-configuration`) {
      await this.json(route, this.metadata());
      return;
    }
    if (url.pathname === "/common/discovery/instance") {
      await this.json(route, {
        tenant_discovery_endpoint: `${ISSUER}/.well-known/openid-configuration`,
        "api-version": "1.1",
        metadata: [
          {
            preferred_network: "login.microsoftonline.com",
            preferred_cache: "login.windows.net",
            aliases: [
              "login.microsoftonline.com",
              "login.windows.net",
              "login.microsoft.com",
              "sts.windows.net",
            ],
          },
        ],
      });
      return;
    }
    if (url.pathname === AUTHORIZE_PATH) {
      await this.authorize(route, request, url);
      return;
    }
    if (url.pathname === TOKEN_PATH && request.method() === "POST") {
      await this.token(route, request);
      return;
    }
    if (url.pathname === LOGOUT_PATH) {
      this.logoutRequests.push(url);
      const target = url.searchParams.get("post_logout_redirect_uri") ?? "about:blank";
      await route.fulfill({ status: 302, headers: { Location: target } });
      return;
    }
    this.unexpected.push(`${request.method()} ${url.href}`);
    await route.fulfill({ status: 404, headers: CORS, body: "not stubbed" });
  }

  private metadata(): Record<string, unknown> {
    return {
      issuer: ISSUER,
      authorization_endpoint: `${LOGIN_ORIGIN}${AUTHORIZE_PATH}`,
      token_endpoint: `${LOGIN_ORIGIN}${TOKEN_PATH}`,
      end_session_endpoint: `${LOGIN_ORIGIN}${LOGOUT_PATH}`,
      jwks_uri: `${LOGIN_ORIGIN}/${TENANT_ID}/discovery/v2.0/keys`,
      userinfo_endpoint: "https://graph.microsoft.com/oidc/userinfo",
      response_modes_supported: ["query", "fragment", "form_post"],
      response_types_supported: ["code", "id_token", "code id_token", "id_token token"],
      scopes_supported: ["openid", "profile", "email", "offline_access"],
      subject_types_supported: ["pairwise"],
      id_token_signing_alg_values_supported: ["RS256"],
      token_endpoint_auth_methods_supported: ["client_secret_post", "private_key_jwt"],
      claims_supported: ["sub", "iss", "aud", "exp", "iat", "nonce", "name", "tid", "oid"],
      http_logout_supported: true,
      frontchannel_logout_supported: true,
      request_uri_parameter_supported: false,
      tenant_region_scope: "EU",
      cloud_instance_name: "microsoftonline.com",
      cloud_graph_host_name: "graph.windows.net",
      msgraph_host: "graph.microsoft.com",
      rbac_url: "https://pas.windows.net",
    };
  }

  private async json(route: Route, body: unknown, status = 200): Promise<void> {
    await route.fulfill({
      status,
      headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
    });
  }

  private async authorize(route: Route, request: Request, url: URL): Promise<void> {
    // A form post carries the same parameters in its body.
    const params =
      request.method() === "POST"
        ? new URLSearchParams(request.postData() ?? "")
        : url.searchParams;
    const recorded = new URL(url.href);
    if (request.method() === "POST") {
      for (const [name, value] of params) recorded.searchParams.set(name, value);
    }
    this.authorizeRequests.push(recorded);
    const redirectUri = params.get("redirect_uri") ?? "about:blank";
    const state = params.get("state") ?? "";
    const outcome = this.outcomes.shift() ?? { kind: "code" };
    const answer = new URLSearchParams();
    if (outcome.kind === "error") {
      answer.set("error", outcome.error);
      answer.set("error_description", outcome.description);
      if (outcome.errorNo !== undefined) {
        answer.set("error_uri", `${LOGIN_ORIGIN}/error?code=${outcome.errorNo}`);
      }
      answer.set("state", state);
    } else {
      const code = base64url(randomBytes(24));
      this.nonces.set(code, params.get("nonce") ?? "");
      answer.set("code", code);
      answer.set("client_info", CLIENT_INFO);
      const separator = state.indexOf("|");
      answer.set(
        "state",
        outcome.kind === "tamper" && separator >= 0
          ? `${state.slice(0, separator + 1)}${outcome.userState}`
          : state,
      );
      answer.set("session_state", "stub-session");
    }
    await route.fulfill({
      status: 302,
      headers: { Location: `${redirectUri}#${answer.toString()}` },
    });
  }

  private async token(route: Route, request: Request): Promise<void> {
    const params = new URLSearchParams(request.postData() ?? "");
    const grantType = params.get("grant_type") ?? "";
    this.tokenRequests.push({ grantType, params, at: Date.now() });
    let nonce: string | null = null;
    if (grantType === "authorization_code") {
      nonce = this.nonces.get(params.get("code") ?? "") ?? null;
      if (nonce === null) {
        await this.json(
          route,
          { error: "invalid_grant", error_description: "AADSTS70008: unknown code" },
          400,
        );
        return;
      }
    } else if (grantType === "refresh_token") {
      const known = (params.get("refresh_token") ?? "").startsWith(`stub-rt.${this.worldId}.`);
      if (this.refreshExpired || !known) {
        await this.json(
          route,
          {
            error: "invalid_grant",
            error_description:
              "AADSTS700084: The refresh token was issued to a single page app (SPA), and therefore has a fixed, limited lifetime.",
            error_codes: [700084],
            suberror: "bad_token",
          },
          400,
        );
        return;
      }
    } else {
      this.unexpected.push(`token grant ${grantType}`);
      await this.json(route, { error: "unsupported_grant_type" }, 400);
      return;
    }
    this.serial += 1;
    const lifetime =
      (this.lifetimes.length > 1 ? this.lifetimes.shift() : this.lifetimes[0]) ?? 3600;
    const now = Math.floor(Date.now() / 1000);
    const idToken = unsignedJwt({
      aud: CLIENT_ID,
      iss: ISSUER,
      iat: now,
      nbf: now,
      exp: now + 3600,
      name: USER_NAME,
      preferred_username: USER_EMAIL,
      oid: IDS.user,
      sub: "stub-subject",
      tid: TENANT_ID,
      ver: "2.0",
      ...(nonce === null ? {} : { nonce }),
    });
    await this.json(route, {
      token_type: "Bearer",
      scope: API_SCOPE,
      expires_in: lifetime,
      ext_expires_in: lifetime,
      access_token: this.accessToken(this.serial),
      ...(this.refreshToken
        ? {
            refresh_token: `stub-rt.${this.worldId}.${String(this.serial)}`,
            refresh_token_expires_in: 86399,
          }
        : {}),
      id_token: idToken,
      client_info: CLIENT_INFO,
    });
  }
}

/**
 * Makes a tampered `state` pass MSAL's own check, so the test reaches the app's return-path rule
 * (section 4.3): when the page returns with a code, the cached authorize request takes the
 * returned `state`. Only the `tamper` outcome needs it; it changes no token or account entry.
 */
export const ACCEPT_RETURNED_STATE = `(() => {
  const hash = new URLSearchParams(window.location.hash.slice(1));
  const state = hash.get("state");
  if (state === null || !hash.has("code")) return;
  const library = state.slice(0, state.indexOf("|") + 1);
  if (library === "") return;
  const decode = (value) =>
    new TextDecoder().decode(Uint8Array.from(atob(value), (c) => c.charCodeAt(0)));
  const encode = (value) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(value)));
  for (let i = 0; i < sessionStorage.length; i += 1) {
    const key = sessionStorage.key(i);
    if (key === null) continue;
    try {
      const request = JSON.parse(decode(sessionStorage.getItem(key) ?? ""));
      if (typeof request.state === "string" && request.state.startsWith(library)) {
        request.state = state;
        sessionStorage.setItem(key, encode(JSON.stringify(request)));
      }
    } catch {
      // Not an encoded request.
    }
  }
})();`;
