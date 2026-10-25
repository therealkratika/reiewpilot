// Harmless helper for ReviewPilot PR validation
export function formatTimestamp(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp).toISOString();
}
