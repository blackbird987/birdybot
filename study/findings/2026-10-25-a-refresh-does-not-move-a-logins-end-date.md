+++
id = "2026-10-25-a-refresh-does-not-move-a-logins-end-date"
title = "A token refresh did not move a login's end date"
date = "2026-10-25"
status = "stands"
question = "Can an account's login be kept alive by using it or refreshing its token?"
verdict = "Not on what was seen: klerk renewed on 2026-10-25 with its end date staying on 2026-11-12, and a default account idle 9 days renewed with its date unchanged. The note concludes a daily ping spends tokens and changes nothing, and does not record a ping being run. Signing in again moves the date."
tags = ["accounts"]
source = { file = "docs/design/multi-account.md", section = "A login has a hard end date, and only a fresh sign-in moves it" }
+++
Worth having before anyone builds a keep-alive for an account that keeps lapsing. Second source: CHANGELOG.md, grepped for the login expiry warning only.

The CLI keeps two tokens per account. The access token lasts 8 hours and is renewed by the CLI from the refresh token. The refresh token's `refreshTokenExpiresAt` is set by the server at sign-in, about four weeks out. What the bot does with that is the note's: it warns 3 days before, and sidelines once the date has passed and the access token in hand has run out too.

What this does not establish. Two observations of renewal, on two accounts, not a test of a keep-alive loop. It says nothing about whether the server's four-week figure is fixed, or about accounts outside `CLAUDE_ACCOUNTS`.
