# Sarkari Naukri Turant bot

This bot checks official recruitment pages every 2 hours: central bodies, Delhi, Haryana, Rajasthan, UP, Bihar, and universities. When a new notice appears, it reads the PDF (using OCR for scanned ones), pulls out the facts, checks every number and date against the notice, and posts a Hindi animated Short for the big vacancies. Every evening at about 8:10 pm IST it posts a long video called "आज की सरकारी भर्तियां" (today's government jobs).

## Files
- `naukri.py`: the bot
- `motion.py`: the animated video engine
- `sources.yaml`: the official pages it watches. Add a line here to watch a new page.
- `config.yaml`: settings
- `requirements.txt`
- `.github/workflows/naukri.yml`: the schedule

## Secrets (repo → Settings → Secrets and variables → Actions)
| Name | Value |
|---|---|
| `GEMINI_API_KEY` | same Gemini key as your other bot |
| `GROQ_API_KEY` | from console.groq.com |
| `CF_ACCOUNT_ID` | Cloudflare account ID (optional backup) |
| `CF_API_TOKEN` | Cloudflare Workers AI token (optional backup) |
| `YT_CLIENT_ID` | Client ID from the **naukri-bot** Google Cloud project |
| `YT_CLIENT_SECRET` | Client secret from the **naukri-bot** project |
| `YT_TOKEN` | refresh token for the **Sarkari Naukri Turant** channel |

## First run
The first real `watch` run only **remembers** the notices already on each site, so it doesn't post hundreds of old ones. From the next check onwards, only **new** notices get videos.

A test run (Test only ticked) instead tries 2 existing notices from each site, so you can see sample videos straight away.

## Good to know
- If a site changes or blocks the bot, the run log lists it under "Sources needing attention".
- Upload limit: about 6 a day per Google project. The bot posts at most 5 (Shorts for the biggest notices, plus the digest).
- Every video description has the official notification link and a "not affiliated" disclaimer.
