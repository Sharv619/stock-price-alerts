# 📈 Stock Price Alerts — What This Is

## The idea (in one sentence)

I built a small app that **watches stock prices for me and sends me a WhatsApp message or email the moment a stock hits the price I care about** — so I don't have to keep checking my phone all day.

## The problem it solves

Say I want to buy Apple stock, but only if it drops below $200. Normally I'd have to keep opening a finance app and checking the price over and over. That's annoying, and it's easy to miss the moment.

With this app, I just say: *"Tell me when Apple goes below $200"* — and then I forget about it. The app checks the price every minute, and the second it happens, I get a message.

## How it works (no jargon)

1. **I open a simple web page** on my computer (the "dashboard").
2. **I type in three things:**
   - Which stock I want to watch (e.g., Apple, Tesla, Google)
   - My target price (e.g., $200)
   - Whether I want to be told when it goes **above** or **below** that price
3. **I pick how I want to be notified** — WhatsApp, email, or both.
4. **The app takes over.** A background helper checks the live stock price every 60 seconds.
5. **When the price crosses my target**, I instantly get a WhatsApp message and/or email.
6. The alert stays active and re-notifies every 60 seconds as long as the condition holds — I delete it from the dashboard when I'm done.

## What I actually built (the pieces)

Think of it like a small team of workers I created:

| Worker | What it does |
|--------|--------------|
| **The dashboard** | The friendly web page where I add and remove my alerts |
| **The brain** | Receives my alerts, saves them, and answers questions like "what's this stock worth right now?" |
| **The watcher** | Wakes up every minute, looks up the latest prices, and checks them against my alerts |
| **The messenger** | Sends the WhatsApp message and email when an alert triggers |
| **The notebook** | A little database file where all my alerts are saved, so nothing is lost if I restart |

## What it uses under the hood (quick version)

- **Live stock prices** come from Yahoo Finance (free, no account needed) for most tickers. **Indian NSE stocks** use DhanHQ's official REST API with automatic TOTP authentication, and fall back to Yahoo Finance when unavailable; BSE remains yfinance-based.
- **WhatsApp messages** are sent through a service called Whapi.
- **Emails** are sent through my Gmail account.
- Everything runs **on my own computer** — no cloud servers, no subscriptions, my data stays with me.

## Why this is neat

- ✅ Works for basically **any stock in the world** that Yahoo Finance knows about
- ✅ Checks prices **every minute, automatically**, even while I do other things
- ✅ Notifies me on **the app I actually look at** (WhatsApp)
- ✅ **Free to run** — no paid stock-data subscriptions
- ✅ Built entirely by me, from the price-watching logic to the web page
