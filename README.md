# PodHub Calling Dashboard

Dashboard + Python voice agent that places real phone calls through VideoSDK.

## What you need (in your OWN VideoSDK / Google accounts)

- `VIDEOSDK_TOKEN`
- A SIP gateway/number (`VIDEOSDK_CALLER_ID`) and a routing rule
  (`VIDEOSDK_ROUTING_RULE_ID`) bound to agent_id `my_telephony_agent`
- `GOOGLE_API_KEY` (Gemini)
- Python **3.12**

> Only run ONE worker per VideoSDK account at a time. Two people running the
> worker with the same token/agent_id will steal each other's calls.
> Real calls cost money. Calls are placed only when `PODHUB_ALLOW_REAL_DIAL=1`.

## Setup on macOS

```bash
brew install python@3.12            # skip if you already have Python 3.12
git clone <repo-url> && cd PodHub-calling
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                # now open .env and fill in your values
bash start.sh
```

Open http://127.0.0.1:8000

To allow real calls, set `PODHUB_ALLOW_REAL_DIAL=1` in `.env` and restart.
Press Ctrl+C in the terminal to stop both backend and worker.

## Setup on Windows

```bat
git clone <repo-url>
cd PodHub-calling
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
rem now open .env and fill in your values
start.bat
```

`start.bat` opens two windows (backend + worker) and enables real dialing
(`PODHUB_ALLOW_REAL_DIAL=1`). Open http://127.0.0.1:8000

## Placing a test call

Mac:
```bash
.venv/bin/python test_call.py "+91XXXXXXXXXX"
```
Windows:
```bat
.venv\Scripts\python.exe test_call.py "+91XXXXXXXXXX"
```
(or just use the Start Call button on the dashboard)

## Optional: setup checks / dev tools

```bash
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip ...
```
Only needed for `test_setup.py`.

## Troubleshooting

- **Call fails immediately with a 400/403 from the provider**: check
  `VIDEOSDK_ROUTING_RULE_ID` and `VIDEOSDK_CALLER_ID` in `.env`; the rule must be
  Active and bound to `my_telephony_agent`.
- **Call marked "failed: Real dialing disabled"**: set `PODHUB_ALLOW_REAL_DIAL=1`.
- **Worker exits with "must be a non-empty ASCII value"**: `GOOGLE_API_KEY` or
  `VIDEOSDK_TOKEN` is missing/has spaces or quotes in `.env`.
- **`.venv not found`**: run the venv + `pip install` steps above first.
