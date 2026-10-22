import assert from "node:assert/strict";
import { test } from "node:test";

import {
  authReducer,
  canAccessProtectedDashboard,
  initialAuthState,
} from "../src/services/authState.js";

test("dashboard remains protected until a user is authenticated", () => {
  assert.equal(canAccessProtectedDashboard(initialAuthState), false);
  const signedIn = authReducer(initialAuthState, {
    type: "authenticated",
    user: { username: "octocat" },
  });
  assert.equal(canAccessProtectedDashboard(signedIn), true);
  assert.equal(signedIn.user.username, "octocat");
});

test("logout and session expiration remove dashboard access", () => {
  const signedIn = authReducer(initialAuthState, {
    type: "authenticated",
    user: { username: "octocat" },
  });
  const loggedOut = authReducer(signedIn, { type: "signed-out" });
  assert.equal(canAccessProtectedDashboard(loggedOut), false);
  const expired = authReducer(signedIn, { type: "session-expired" });
  assert.equal(canAccessProtectedDashboard(expired), false);
  assert.equal(expired.expired, true);
});
