import { useCallback, useEffect, useReducer } from "react";
import Dashboard from "./pages/Dashboard";
import { apiBaseUrl, getCurrentUser, logout } from "./services/api";
import {
  authReducer,
  canAccessProtectedDashboard,
  initialAuthState,
} from "./services/authState";

function SignIn({ error, expired, onRetry }) {
  return (
    <main className="auth-page">
      <section className="auth-card">
        <div className="brand"><span className="brand-mark">R</span><span>ReviewPilot</span></div>
        <h1>{expired ? "Your session expired" : "Sign in to ReviewPilot"}</h1>
        <p>Connect with GitHub to review repositories you are authorized to access.</p>
        {error && <div className="error-banner" role="alert">{error}</div>}
        <a className="primary-button auth-button" href={`${apiBaseUrl}/v1/auth/login`}>
          Continue with GitHub
        </a>
        {error && <button className="auth-retry" onClick={onRetry}>Retry session check</button>}
      </section>
    </main>
  );
}

export default function App() {
  const [auth, dispatch] = useReducer(authReducer, initialAuthState);

  const refreshSession = useCallback(async () => {
    dispatch({ type: "loading" });
    try {
      const currentUser = await getCurrentUser();
      dispatch(currentUser
        ? { type: "authenticated", user: currentUser }
        : { type: "signed-out" });
    } catch (sessionError) {
      dispatch({ type: "error", message: sessionError.message });
    }
  }, []);

  useEffect(() => {
    void refreshSession();
  }, [refreshSession]);

  useEffect(() => {
    function handleExpired() {
      dispatch({ type: "session-expired" });
    }
    window.addEventListener("reviewpilot:session-expired", handleExpired);
    return () => window.removeEventListener("reviewpilot:session-expired", handleExpired);
  }, []);

  async function handleLogout() {
    await logout();
    dispatch({ type: "signed-out" });
  }

  if (auth.status === "loading") {
    return <main className="auth-page"><p role="status">Checking your ReviewPilot session...</p></main>;
  }
  if (!canAccessProtectedDashboard(auth)) {
    return <SignIn error={auth.error} expired={auth.expired} onRetry={refreshSession} />;
  }
  return <Dashboard user={auth.user} onLogout={handleLogout} />;
}
