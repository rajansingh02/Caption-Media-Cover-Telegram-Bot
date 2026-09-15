import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client

from .config import API_HASH, API_ID, BOT_TOKEN, SESSION_NAME
from .handlers import callbacks, commands, media

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

commands.register(app)
media.register(app)
callbacks.register(app)

if __name__ == "__main__":
    print("Caption bot is running...")
    app.run()
