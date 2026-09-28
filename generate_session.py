#!/usr/bin/env python3
"""
Run this ONCE to create your session string (the SESSION_STRING secret used
when hosting on GitHub Actions, or on any machine where you cannot log in
interactively).

Works on PC and on Android via Termux:

    pkg update -y && pkg install -y python git
    git clone https://github.com/vot1122/group-watchdog
    cd group-watchdog
    pip install telethon
    python generate_session.py

It prints the string AND saves it to session.txt. Keep it private - it is
equivalent to being logged in to your account. Never screenshot or share it.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

print("\nLog in with the phone number you use in the group (international")
print("format, e.g. +91XXXXXXXXXX), then enter the code Telegram sends to")
print("your Telegram app.\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()

with open("session.txt", "w", encoding="utf-8") as fh:
    fh.write(session_string + "\n")

print("\n=== SESSION_STRING (copy the ENTIRE line) ===\n")
print(session_string)
print("\n=== END ===")
print("\nAlso saved to session.txt. To copy it straight to the clipboard in Termux:")
print("    cat session.txt | termux-clipboard-set")
print("(that needs: pkg install termux-api, plus the 'Termux:API' app installed)")
