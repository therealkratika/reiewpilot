// Harmless helper function for testing ReviewPilot PR review pipeline
export function sanitizeText(text) {
  if (typeof text !== "string") return "";
  return text.trim();
}
