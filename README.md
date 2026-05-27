# Roofz Maastricht monitor

## What it does
- Checks the Roofz Maastricht listing page on a schedule
- Detects new project pages
- Sends a Telegram message when something new appears

## Free deployment recommendation
Use **GitHub Actions** on a **public repository** if you want the closest thing to zero cost. GitHub documents that standard GitHub-hosted runners are free and unlimited on public repositories, and scheduled workflows are supported. Telegram's Bot API is free of charge, and Cloudflare Workers has a Free plan if you later want to move the bridge or logic there.

## Setup
1. Create a Telegram bot with `@BotFather`.
2. Send a message to your bot from your own Telegram account.
3. Get your chat id:
   - easiest: use a helper bot or a small script with `getUpdates`
   - or use a channel and set the chat id to `@yourchannelname`
4. Put your token and chat id into GitHub repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Commit this repo and let GitHub Actions run every 5 minutes.

## First run behavior
The script seeds the current state on the first run and does not alert immediately. That prevents a flood of notifications. If you want an alert even on the first run, set `ALERT_ON_FIRST_RUN=1`.

## Notes
- The script prefers Playwright because the site may render content dynamically.
- If Playwright turns out to be unnecessary, you can remove the install step and let the script use plain requests.
