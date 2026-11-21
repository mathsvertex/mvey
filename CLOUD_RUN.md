# Deploy on Google Cloud Run (console)

## 0. One-time Google setup
1. console.cloud.google.com: create or pick a project and make sure **billing is linked** (Cloud Run's free tier still needs it).
2. Enable these APIs: **Cloud Run Admin**, **Cloud Build**, **Artifact Registry**, **Google Sheets API**.
3. IAM & Admin > Service Accounts > Create service account (for the sheet). Open it > Keys > Add key > JSON. Share the Google Sheet with its email as **Editor**. Keep the JSON file; you will paste it as one line below.

## 1. Free Redis (needed, because Cloud Run forgets everything when it scales to zero)
Create a free database at upstash.com (Redis). Copy its connection URL. It looks like `rediss://default:PASSWORD@HOST:6379`. This is `REDIS_URL`.

## 2. Create the service
Cloud Run > **Create service** > **Continuously deploy from a repository** > Set up with Cloud Build:
- Connect GitHub and select `mathsvertex/mvey`, branch `main`.
- Build type: **Dockerfile**, source location `/Dockerfile`.

Then set:

| Setting | Value |
|---|---|
| Region | Pick one near you (for example asia-south1, Mumbai) |
| Authentication | **Allow unauthenticated invocations** (Meta must be able to reach the webhook) |
| CPU allocation | **CPU is only allocated during request processing** (the default) |
| Minimum instances | **0** |
| Maximum instances | **1** |
| Request timeout | 120 seconds |
| Memory | 512 MiB |

Under **Containers > Variables & Secrets**, add:

| Name | Value |
|---|---|
| `INLINE_PROCESSING` | `1` |
| `WHATSAPP_PHONE_NUMBER_ID` | `4943710932571546` |
| `SHEET_ID` | `1YcAHU3WVAvpAPvuOGwo7bfyk6otMZupu5HkppmOxmNA` |
| `WHATSAPP_VERIFY_TOKEN` | any random string you make up |
| `WHATSAPP_TOKEN` | new system-user token |
| `META_APP_SECRET` | new app secret |
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the service-account JSON on one line |
| `REDIS_URL` | the Upstash `rediss://...` URL |

For the secrets (token, app secret, API key, JSON, Redis URL), prefer **Reference a secret** (Secret Manager) over plain variables.

Click **Create**. When the deploy finishes, the page shows the service URL, like `https://maths-vertex-agent-xxxx.a.run.app`.

## 3. Connect Meta
WhatsApp > Configuration > Webhook:
- Callback URL: `https://YOUR-SERVICE-URL/webhook`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` you chose
- Verify and Save, then subscribe to **messages**.

## 4. Test
Message the number from a phone. Cloud Run > your service > **Logs** shows each message. A first message after a quiet spell takes a few extra seconds while the instance wakes up.

## Why these settings
- `INLINE_PROCESSING=1`: Cloud Run slows the CPU once a response is sent, so the app writes the reply before answering Meta. If Meta retries a slow call, the app ignores the repeat.
- Maximum instances 1: the app keeps its per-parent reply lock in the process.
- Minimum instances 0 keeps it inside the free tier. Setting it to 1 removes the wake-up delay but is billed.
