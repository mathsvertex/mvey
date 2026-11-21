# Maths Vertex WhatsApp agent (WhatsApp Cloud API)

Same agent as the CodeWords version, running on Meta's official WhatsApp Business Cloud API.
Parents message the business number, Meta calls `POST /webhook`, and the agent replies. It answers from
the FAQ tab, asks Grade, Board and challenges, books demos from the Slots tab, and writes every lead to the Leads tab.

Google Sheet tabs and columns are unchanged: Leads (A:K, K = Notes), Slots (A:E), FAQ (A:B).

## Read this first: the phone number

The Cloud API takes over a number. You have two routes:

1. **Dedicated number (recommended to start).** Register a new number for the Cloud API. Nothing changes for +918858079444 and you can run both side by side while you test.
2. **Move +918858079444.** Registering it for the Cloud API disconnects it from the WhatsApp app on the phone, so the team can no longer reply from that phone. The old chats on the phone are not carried over.

Meta's "coexistence" (app and API on one number) exists, but Meta's onboarding docs say it is set up through Embedded Signup and require the setup to be done as a Solution Partner or Tech Provider, so I would not plan on it for a single business. If you get it, the code already handles it: a reply typed in the app pauses the bot for that parent for 2 hours.

Other differences from the device-link version:

- The bot can only reply freely within 24 hours of the parent's last message. That fits an inbound-first bot. Contacting parents first would need approved templates, which stays out of scope.
- It cannot read chats from before the number was on the Cloud API. For your past demo-done leads, put the context in the **Notes** column and set Status to something like `Demo Done`. The agent uses it exactly as before.
- Without coexistence, a person taking over a chat sets that parent's **Status to `Human Handling`** in the Leads tab. The bot then stays silent for that parent and still logs their messages in Last Message. Set Status back to anything else to hand the chat back.

## 1. Meta setup

1. Go to developers.facebook.com, create an app of type **Business**, and add the **WhatsApp** product.
2. Under WhatsApp > API Setup, add your phone number and verify it. Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
3. In Business Settings > System users, create a system user, assign it your app and WhatsApp account, and generate a token with `whatsapp_business_messaging` and `whatsapp_business_management`. This is the permanent token for `WHATSAPP_TOKEN`. The temporary token in API Setup expires in about 24 hours, so do not use it.
4. App Settings > Basic: copy the **App Secret** into `META_APP_SECRET`.
5. Choose any random string for `WHATSAPP_VERIFY_TOKEN`.
6. To message real parents (not only test numbers), complete Meta business verification and switch the app to Live mode.

## 2. Google Sheets access

1. In Google Cloud Console create a project, enable the **Google Sheets API**, and create a **service account** with a JSON key.
2. Open the sheet and **Share** it with the service account's email (it looks like `name@project.iam.gserviceaccount.com`) as Editor.
3. Put the whole JSON key on one line in `GOOGLE_SERVICE_ACCOUNT_JSON` (or base64 of it).

This uses your own Google quota, so the shared-quota 429 errors from the Composio setup go away.

## 3. Deploy

Any host that runs a Docker container 24/7 works (Render, Railway, Fly.io, a VPS). Set the variables from `.env.example`, use the included `Dockerfile`, and keep **one instance** (the app keeps a per-parent reply lock in process).

Add a Redis instance and set `REDIS_URL`. Without it, conversation memory and the 2-hour pauses are lost on every restart. The sheet still holds each lead's details and notes.

Local run:

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
uvicorn main:app --port 8000
```

## 4. Connect Meta to the app

1. In the Meta dashboard, WhatsApp > Configuration > Webhook: set the callback URL to `https://YOUR-HOST/webhook` and the verify token to your `WHATSAPP_VERIFY_TOKEN`, then Verify and Save.
2. Subscribe to the **messages** field. If you use coexistence, also subscribe to **smb_message_echoes**.
3. Send a WhatsApp message to the number from a phone and watch the logs.

## Behaviour (unchanged from the CodeWords agent)

- DMs only, text only. Voice notes and images get a short "please type your question" reply.
- Greets with just "Hi", never uses the parent's name, replies in the parent's language (English, Hindi or Hinglish), times in UAE time.
- Answers only from the FAQ tab; anything else is flagged for the team (Status `Needs Follow-up`, plus a dated note).
- STOP / UNSUBSCRIBE / "remove me" opts out; START opts back in.
- Duplicate deliveries are ignored, and so are messages older than 30 minutes (Meta redelivers after downtime). Change with `STALE_AFTER_SECONDS`.
- Returning parents: uses Status, Demo Slot, Notes and the last 16 turns of conversation, never re-asks what it knows, never invents what happened in a demo.

## Turning off the CodeWords version

Once this one is live and tested, unsubscribe the old webhook so two agents never answer the same parent. The CodeWords agent works on the linked device number, so the two only collide if both numbers are the same one.

## Deploying on Render

Push this folder to a private GitHub repo, then in Render choose New > Blueprint and pick the repo. `render.yaml` creates the web service and a Key Value store, and asks you for the secrets. After the first deploy, open the service and copy `WHATSAPP_VERIFY_TOKEN` into the Meta webhook settings (step 4 above).

### Free hosting

The blueprint uses Render's free plan. Free web services sleep after about 15 minutes idle, so create a free monitor at uptimerobot.com that requests `https://<your-service>.onrender.com/` every 5 minutes. For real parent traffic, change `plan: free` to `plan: starter` in `render.yaml`.
