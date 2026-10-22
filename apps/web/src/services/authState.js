export const initialAuthState = {
  status: "loading",
  user: null,
  error: "",
  expired: false,
};

export function authReducer(state, action) {
  switch (action.type) {
    case "loading":
      return { ...state, status: "loading", error: "" };
    case "authenticated":
      return { status: "authenticated", user: action.user, error: "", expired: false };
    case "signed-out":
      return { status: "signed_out", user: null, error: "", expired: false };
    case "session-expired":
      return { status: "signed_out", user: null, error: "", expired: true };
    case "error":
      return { status: "error", user: null, error: action.message, expired: false };
    default:
      return state;
  }
}

export function canAccessProtectedDashboard(state) {
  return state.status === "authenticated" && state.user !== null;
}
