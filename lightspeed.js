// Self-sufficient on config: this module reads LS_CLIENT_ID/SECRET directly, so it
// loads dotenv itself rather than relying on whichever entrypoint imported it.
// Without this, a script that imports this module without dotenv sends EMPTY
// credentials and Lightspeed replies "invalid_client" — which reads like revoked
// keys and sends you chasing the wrong problem entirely.
import 'dotenv/config';
import fetch from 'node-fetch';
import { getTokens, saveTokens } from './db.js';

const TOKEN_URL = 'https://cloud.lightspeedapp.com/oauth/access_token.php';
const AUTHORIZE_URL = 'https://cloud.lightspeedapp.com/oauth/authorize.php';

// Fail loudly and specifically instead of letting the API reject empty credentials.
function requireCredentials() {
  const missing = ['LS_CLIENT_ID', 'LS_CLIENT_SECRET'].filter(k => !process.env[k]);
  if (missing.length) {
    throw new Error(`Missing ${missing.join(' and ')} — check .env in the project root. ` +
      `(Lightspeed reports absent credentials as "invalid_client", which looks like revoked keys.)`);
  }
}

export function buildAuthorizeUrl() {
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: process.env.LS_CLIENT_ID,
    redirect_uri: process.env.LS_REDIRECT_URI,
    scope: 'employee:all',
    state: 'ls-dashboard'
  });
  return `${AUTHORIZE_URL}?${params.toString()}`;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/*
 * Every network call goes through this. Access tokens last only 1800s, so a
 * multi-hour sync refreshes every ~30 min; a transient DNS/connection blip on the
 * TOKEN endpoint
 * is just as fatal as one on a data endpoint — an earlier run died exactly that
 * way (ENOTFOUND on access_token.php) after the data path alone was hardened.
 * Retries transport errors and 5xx/429 with exponential backoff; a 4xx is a real
 * answer from the server and is returned to the caller unretried.
 */
async function fetchWithRetry(url, options = {}, label = 'request') {
  let lastErr;
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      const res = await fetch(url, options);
      if (res.status === 429 || res.status >= 500) {
        const retryAfter = Number(res.headers.get('Retry-After')) || Math.min(2 ** attempt, 30);
        await sleep(retryAfter * 1000);
        continue;
      }
      return res;
    } catch (err) {
      lastErr = err;
      await sleep(Math.min(2 ** attempt, 30) * 1000);
    }
  }
  throw new Error(`${label} failed after retries: ${lastErr?.message ?? 'exhausted'}`);
}

async function fetchAccountId(accessToken) {
  const res = await fetchWithRetry('https://api.lightspeedapp.com/API/Account.json', {
    headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' }
  }, 'Account lookup');
  if (!res.ok) throw new Error(`Account lookup failed: ${res.status} ${await res.text()}`);
  const json = await res.json();
  return json.Account.accountID;
}

export async function exchangeCodeForToken(code) {
  requireCredentials();
  const res = await fetchWithRetry(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: process.env.LS_CLIENT_ID,
      client_secret: process.env.LS_CLIENT_SECRET,
      redirect_uri: process.env.LS_REDIRECT_URI,
      code
    })
  });
  if (!res.ok) throw new Error(`Token exchange failed: ${res.status} ${await res.text()}`);
  const json = await res.json();
  const accountId = await fetchAccountId(json.access_token);
  saveTokens({
    accountId,
    accessToken: json.access_token,
    refreshToken: json.refresh_token,
    expiresAt: Date.now() + json.expires_in * 1000
  });
  return json;
}

async function refreshAccessToken(refreshToken) {
  requireCredentials();
  const res = await fetchWithRetry(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'refresh_token',
      client_id: process.env.LS_CLIENT_ID,
      client_secret: process.env.LS_CLIENT_SECRET,
      refresh_token: refreshToken
    })
  }, 'Token refresh');
  if (!res.ok) throw new Error(`Token refresh failed: ${res.status} ${await res.text()}`);
  const json = await res.json();
  const current = getTokens();
  const accountId = current.account_id ?? await fetchAccountId(json.access_token);
  saveTokens({
    accountId,
    accessToken: json.access_token,
    refreshToken: json.refresh_token ?? refreshToken,
    expiresAt: Date.now() + json.expires_in * 1000
  });
  return json.access_token;
}

export async function getValidAccessToken() {
  const tokens = getTokens();
  if (!tokens) throw new Error('Not authorized yet — visit /oauth/authorize first.');
  if (Date.now() < tokens.expires_at - 60_000) return { accessToken: tokens.access_token, accountId: tokens.account_id };
  const accessToken = await refreshAccessToken(tokens.refresh_token);
  return { accessToken, accountId: tokens.account_id };
}

// Used when the API rejects a token we still thought was valid (clock drift, revocation, etc.).
export async function forceTokenRefresh() {
  const tokens = getTokens();
  if (!tokens) throw new Error('Not authorized yet — visit /oauth/authorize first.');
  return refreshAccessToken(tokens.refresh_token);
}

// R-Series rate-limits via a "leaky bucket" (Retry-After / X-LS-API-Bucket-Level headers).
// Simple, safe backoff: retry on 429/503 honoring Retry-After.
// Fetches a fresh (auto-refreshed) token on every call — long-running syncs can outlive one token's lifetime.
export async function lsFetch(path) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const { accountId, accessToken } = await getValidAccessToken();
    const url = path.startsWith('http') ? path : `https://api.lightspeedapp.com/API/V3/Account/${accountId}${path}`;
    let res;
    try {
      res = await fetch(url, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' }
      });
    } catch (err) {
      // Transient network errors (ETIMEDOUT, ECONNRESET, ENOTFOUND) — backoff and retry.
      await sleep(Math.min(2 ** attempt, 30) * 1000);
      continue;
    }
    if (res.status === 401) {
      await forceTokenRefresh();
      continue;
    }
    if (res.status === 429 || res.status >= 500) {
      const retryAfter = Number(res.headers.get('Retry-After')) || Math.min(2 ** attempt, 30);
      await sleep(retryAfter * 1000);
      continue;
    }
    if (!res.ok) throw new Error(`Lightspeed API error ${res.status}: ${await res.text()}`);
    return res.json();
  }
  throw new Error(`Lightspeed API: exhausted retries for ${path}`);
}

// Fetch all pages of a resource. R-Series paginates via @attributes.next (a full URL), not offset.
export async function lsFetchAll(path, resourceKey, pageSize = 100, onPage) {
  const sep = path.includes('?') ? '&' : '?';
  let next = `${path}${sep}limit=${pageSize}`;
  const all = [];
  while (next) {
    const json = await lsFetch(next);
    const items = json[resourceKey];
    const batch = Array.isArray(items) ? items : items ? [items] : [];
    all.push(...batch);
    onPage?.(batch, all.length);
    next = json['@attributes']?.next || null;
  }
  return all;
}
