Render deployment notes
1. Back up /var/data/config.json and Telegram session before deployment.
2. This ZIP contains sanitized config.json, no credentials. The existing persistent /var/data/config.json is not overwritten by server startup.
3. IMPORTANT: If /var/data/config.json is invalid, correct it using a private secure method; this ZIP cannot repair it automatically.
4. Set BOT_DATA_DIR=/var/data and mount persistent disk there.
5. Rotate previously exposed Pocket Option session and remove it from Git history (gitignore does not erase history).
6. Keep Demo, disable Martingale while testing, start dashboard, connect Pocket Option, then bot.
7. If handshake still fails, capture sanitized Render logs and installed pocketoptionapi_async version. No live connection test has been performed.
