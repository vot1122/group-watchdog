#!/usr/bin/env python3
"""
Run this ONCE on your own computer (Windows/Mac/Linux) - NOT on GitHub.

It logs into Telegram interactively (phone + code from the app) and prints a
StringSession: one long line that is your complete login. You paste that line
into your GitHub repo as the secret SESSION_STRING.

    pip install telethon
    python generate_session.py

Keep the printed string private - it is equivalent to being logged in.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

print("\nLog in with the phone number you use in the group (international format,")
print("e.g. +91XXXXXXXXXX), then enter the code Telegram sends to your app.\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()

print("\n=== SESSION_STRING (copy the ENTIRE line below) ===\n")
print(session_string)
print("\n=== END - now add it as the repo secret named SESSION_STRING ===")
