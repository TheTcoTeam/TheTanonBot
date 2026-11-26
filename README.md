# TheTanonBot

A Telegram inbox bot that presents an anonymous-style inbox to senders while showing the bot owner each sender's name, `@username`, clickable profile link, and numeric Telegram user ID.

## Features
- **Sender identity visible to owner**: Displays full name, `@username`, and user ID for every incoming message.
- **Supports text & media**: Photos, videos, voice notes, stickers, documents, etc.
- **Direct replies**: Reply to any forwarded message in Telegram to answer the sender.
- **Moderation & stats**: `/stats`, `/block <user_id>`, `/unblock <user_id>`.
- **Render Web Service ready**: Automatically uses Telegram webhooks when `WEBHOOK_URL` or `RENDER_EXTERNAL_URL` is set, and falls back to polling locally.
