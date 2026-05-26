import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { apiRequest, loginRequest } from "./msal-config";
import { fetchUserProfile, type UserProfile } from "./graph-service";
import { ApiError, apiClient, setTokenGetter } from "../api/client";

interface AuthState {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: UserProfile | null;
  token: string | null;
  role: string;
  noGroup: boolean;
  login: () => void;
  logout: () => void;
}

const AuthContext = createContext<AuthState>({
  isAuthenticated: false,
  isLoading: true,
  user: null,
  token: null,
  role: "user",
  noGroup: false,
  login: () => {},
  logout: () => {},
});

export function useAuth() {
  return useContext(AuthContext);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const { instance, accounts, inProgress } = useMsal();
  const isMsalAuthenticated = useIsAuthenticated();
  const [user, setUser] = useState<UserProfile | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [role, setRole] = useState("user");
  const [isLoading, setIsLoading] = useState(true);
  const [noGroup, setNoGroup] = useState(false);

  // Mirror the token into a ref so apiClient's registered getter reads the
  // current value at call time, not whatever was captured at registration.
  const tokenRef = useRef<string | null>(null);
  useEffect(() => {
    tokenRef.current = token;
  }, [token]);
  useEffect(() => {
    setTokenGetter(() => tokenRef.current);
    return () => setTokenGetter(null);
  }, []);

  // Check for magic link token in localStorage
  const magicToken = localStorage.getItem("rag_auth_token");
  const isAuthenticated = isMsalAuthenticated || !!magicToken;

  useEffect(() => {
    async function initAuth() {
      if (isMsalAuthenticated && accounts.length > 0) {
        try {
          // Graph token: used locally to call /me for the user profile.
          const graphResponse = await instance.acquireTokenSilent({
            ...loginRequest,
            account: accounts[0],
          });
          const graphToken = graphResponse.accessToken;

          const profile = await fetchUserProfile(graphToken);
          setUser(profile);

          // API token: different audience, sent as the bearer to backends.
          // Backends validate it via JWKS and read the groups claim for the
          // platform role; see ADR-0003.
          const apiResponse = await instance.acquireTokenSilent({
            ...apiRequest,
            account: accounts[0],
          });
          const apiToken = apiResponse.accessToken;

          const result = await apiClient("/users/provision", {
            method: "POST",
            body: JSON.stringify({ display_name: profile.displayName }),
            token: apiToken,
          });
          setToken(apiToken);
          setRole(result.role || "user");
          // Clear the lockout in case a previous attempt set it: group
          // membership may have changed since.
          setNoGroup(false);
        } catch (err) {
          if (err instanceof ApiError && err.code === "no_group") {
            // Authenticated against Azure AD but in neither group; ADR-0003.
            setNoGroup(true);
          } else {
            console.error("Auth init failed:", err);
          }
        }
      } else if (magicToken) {
        try {
          const payload = JSON.parse(atob(magicToken.split(".")[1]));
          setUser({ displayName: payload.display_name, mail: payload.sub, id: "" });
          setToken(magicToken);
          setRole(payload.role || "guest");
        } catch {
          localStorage.removeItem("rag_auth_token");
        }
      } else {
        // No MSAL session and no magic-link token: clear any prior auth so
        // apiClient does not keep sending a stale bearer after sign-out or
        // session loss.
        tokenRef.current = null;
        setToken(null);
        setUser(null);
        setRole("user");
        setNoGroup(false);
      }
      setIsLoading(false);
    }

    if (inProgress === InteractionStatus.None) {
      initAuth();
    }
  }, [isMsalAuthenticated, accounts, inProgress, instance, magicToken]);

  const login = useCallback(() => {
    instance.loginRedirect(loginRequest);
  }, [instance]);

  const logout = useCallback(() => {
    // Clear auth state synchronously, including the ref, so apiClient stops
    // sending the bearer in the window before the redirect resolves.
    localStorage.removeItem("rag_auth_token");
    tokenRef.current = null;
    setToken(null);
    setUser(null);
    setRole("user");
    setNoGroup(false);
    if (isMsalAuthenticated) {
      instance.logoutRedirect();
    } else {
      window.location.href = "/login";
    }
  }, [instance, isMsalAuthenticated]);

  return (
    <AuthContext.Provider
      value={{ isAuthenticated, isLoading, user, token, role, noGroup, login, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}
