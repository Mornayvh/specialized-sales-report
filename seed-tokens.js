/*
 * Bootstraps the `tokens` table from a refresh token supplied via environment
 * variable, for a fresh/ephemeral data.sqlite that has never been through the
 * interactive /oauth/authorize flow (i.e. a GitHub Actions runner).
 *
 * expiresAt is deliberately set in the past: getValidAccessToken() (lightspeed.js)
 * sees an expired token and immediately calls refreshAccessToken(), which mints a
 * real access token AND resolves accountId via the Account endpoint — so a bare
 * refresh token is all this needs to fully bootstrap a working session.
 *
 * Run with: LS_REFRESH_TOKEN=... node seed-tokens.js
 */
import { saveTokens } from './db.js';

const refreshToken = process.env.LS_REFRESH_TOKEN;
if (!refreshToken) {
  console.error('LS_REFRESH_TOKEN is not set — nothing to seed.');
  process.exit(1);
}

saveTokens({
  accountId: null,
  accessToken: '',
  refreshToken,
  expiresAt: 0,
});

console.log('Seeded tokens table from LS_REFRESH_TOKEN (account ID will resolve on first refresh).');
