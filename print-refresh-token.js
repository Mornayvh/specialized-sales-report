/*
 * Prints ONLY the current refresh_token to stdout, nothing else — for piping
 * straight into `gh secret set` at the end of the daily-sync workflow so a
 * rotated token (if Lightspeed ever issues one on refresh) gets written back
 * without ever appearing in a log line. See db.js/lightspeed.js for how
 * refreshAccessToken() decides whether to rotate.
 */
import { getTokens } from './db.js';

const tokens = getTokens();
if (!tokens?.refresh_token) {
  console.error('No refresh_token in data.sqlite — sync must run before this.');
  process.exit(1);
}
process.stdout.write(tokens.refresh_token);
