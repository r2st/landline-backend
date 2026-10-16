# Gmail OAuth Setup — Per-User Email Sending

Landline can send outreach email **as each connected user** through the Gmail
API instead of (or in addition to) Amazon SES. Sending through Gmail means each
message goes out via Google's infrastructure with that domain's SPF/DKIM/DMARC
alignment, which is what keeps it out of recipients' spam folders.

There is no separate Landline login — **connecting a Gmail account *is* the
identity**. Vidhi connects her Google account and her outreach sends from her
address; Suman connects his and sends from his. The "active" connected account
is the default sender; the compose screen can also send as any other connected
account.

This document covers the one-time Google Cloud configuration. Everything the app
needs is supplied via environment variables — no secrets are committed.

---

## 1. Create / select a Google Cloud project

1. Go to <https://console.cloud.google.com/>.
2. Create a project (e.g. **Landline Outreach**) or select an existing one.

## 2. Enable the Gmail API

1. **APIs & Services → Library**.
2. Search for **Gmail API** → **Enable**.

## 3. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type:
   - **Internal** if all senders are in the same Google Workspace org
     (simplest — no verification needed, no test-user list).
   - **External** otherwise. While in *Testing* status, add each sender
     (vidhi@…, suman@…) under **Test users**. To go live without the
     100-user/test-user cap and the "unverified app" screen, submit for
     verification (required because `gmail.send` is a restricted scope).
3. App name: **Landline**, support email, developer contact.
4. **Scopes** — add:
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/userinfo.profile`
   - `https://www.googleapis.com/auth/gmail.send`  ← the restricted send scope

## 4. Create the OAuth client credentials

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. **Authorized redirect URIs** — add the backend callback. It must match
   `GOOGLE_OAUTH_REDIRECT_URI` exactly:
   - Production:  `https://prospect.doaide.com/api/v1/gmail/oauth/callback`
   - Local dev:   `http://localhost:8000/api/v1/gmail/oauth/callback`
4. Copy the generated **Client ID** and **Client Secret**.

## 5. Set the environment variables

Add these to `/opt/landline/.env.production` (and your local `.env`):

```dotenv
GOOGLE_CLIENT_ID=<client id from step 4>
GOOGLE_CLIENT_SECRET=<client secret from step 4>
GOOGLE_OAUTH_REDIRECT_URI=https://prospect.doaide.com/api/v1/gmail/oauth/callback
FRONTEND_URL=https://prospect.doaide.com
TOKEN_ENCRYPTION_KEY=<run the command below once and paste the result>
```

Generate the token-encryption key once (used to encrypt stored OAuth tokens at
rest — keep it stable; rotating it invalidates all stored connections):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then restart the API + worker:

```bash
cd /opt/landline && docker compose -f docker-compose.prod.yml up -d
```

---

## How a user connects (runtime)

1. In the app, open **Settings → Email Sending** and click **Connect Gmail**.
2. A Google popup opens; the user signs in and grants the **Send email** scope.
3. The popup closes and the account appears as **Connected** and **Active**.
4. Outreach (compose + sequences) now sends as that Gmail address. With no
   connected account, Landline falls back to SES (`SES_SENDER_EMAIL`).

## Notes

- **Scope is send-only.** `gmail.send` cannot read mail or contacts.
- **Refresh tokens** are requested with `access_type=offline&prompt=consent`,
  so re-connecting an account always refreshes the stored token.
- **Disconnecting** revokes the token at Google and deletes it locally.
- If Google ever rejects a send (revoked access, expired grant), the account is
  flagged `error` in the UI and sends fall back to SES until it's reconnected.
