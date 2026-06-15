#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Toolbox — an interactive tool that runs with your personal account.

Run it and a numbered menu appears; pick a number and type the inputs:
    python telegram_tool.py
or double-click run.bat

Needs a .env file with TG_API_ID, TG_API_HASH, TG_PHONE
(get them from https://my.telegram.org).
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

# Resolve files (.env, session, scheduled runner) relative to this script,
# so scheduled/automated runs work no matter the working directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_NAME = "TelegramAutoCleanup"

# ---------------------------------------------------------------------------
# Force UTF-8 on Windows consoles so output renders correctly
# ---------------------------------------------------------------------------
def _setup_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


_setup_utf8()

try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
except ImportError:
    sys.exit(
        "Telethon is not installed. Install it with:\n"
        "    pip install -r requirements.txt\n"
        "or:  pip install telethon"
    )


# ===========================================================================
# Config & helpers
# ===========================================================================
def load_env(path=None):
    """Load settings from a .env file (no external libraries)."""
    if path is None:
        path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_date(text, end_of_day=False):
    """Parse a date in many formats: 2026-06-10 / 10/6 / 10/6/2026 / 10-6-2026."""
    text = text.strip()
    local_tz = datetime.now().astimezone().tzinfo
    today = datetime.now()

    candidates = [
        ("%Y-%m-%d", None),
        ("%d/%m/%Y", None),
        ("%d/%m", today.year),
        ("%d-%m-%Y", None),
        ("%d-%m", today.year),
        ("%Y/%m/%d", None),
    ]
    parsed = None
    for fmt, default_year in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            if default_year is not None:
                dt = dt.replace(year=default_year)
            parsed = dt
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(
            f"Could not understand the date '{text}'. "
            "Use a format like 2026-06-10 or 10/6 or 10/6/2026."
        )

    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed.replace(tzinfo=local_tz)


def parse_datetime(text):
    """Date + optional time: 2026-06-20 or 2026-06-20 14:30 or 20/6 14:30."""
    text = text.strip()
    local_tz = datetime.now().astimezone().tzinfo
    today = datetime.now()
    candidates = [
        "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m %H:%M",
        "%Y-%m-%d", "%d/%m/%Y", "%d/%m",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=today.year)
            return dt.replace(tzinfo=local_tz)
        except ValueError:
            continue
    raise ValueError(f"Could not understand the date/time '{text}'.")


# ---------------------------------------------------------------------------
# Interactive input
# ---------------------------------------------------------------------------
def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else (default if default is not None else "")


def ask_int(prompt, default=None, minimum=None):
    while True:
        raw = ask(prompt, default)
        try:
            n = int(raw)
            if minimum is not None and n < minimum:
                print(f"  Must be {minimum} or greater.")
                continue
            return n
        except (ValueError, TypeError):
            print("  Enter a whole number.")


def ask_yes_no(prompt, default=True):
    d = "yes" if default else "no"
    while True:
        raw = ask(f"{prompt} (yes/no)", d).lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Answer yes or no.")


def ask_revoke():
    """Ask: delete for everyone, or only on my side."""
    print("\n  Deletion mode:")
    print("    1) For everyone  - removed for you AND the other side (default)")
    print("    2) Only for me   - removed on your side only")
    choice = ask("  Choose (1/2)", "1")
    return choice != "2"  # True = revoke (for everyone)


def confirm_destructive(count, strong=False):
    """Confirm before deleting. strong=True requires typing a word."""
    if count == 0:
        print("Nothing to delete.")
        return False
    if strong:
        print(f"\n!!  You are about to delete {count} messages - this cannot be undone!")
        word = ask("Type DELETE to confirm, or anything else to cancel")
        return word.strip() == "DELETE"
    return ask_yes_no(f"\nConfirm deleting {count} messages?", default=False)


# ===========================================================================
# Core Telegram operations
# ===========================================================================
async def list_dialogs(client, limit=40, name_filter=None):
    print(f"\n{'Matching chats' if name_filter else 'Chats'}:\n" + "-" * 70)
    shown = 0
    async for d in client.iter_dialogs(limit=None if name_filter else limit):
        if name_filter and name_filter.lower() not in (d.name or "").lower():
            continue
        kind = "channel" if d.is_channel else "group" if d.is_group else "private"
        print(f"  id={d.id:<16} [{kind:<8}] {d.name or '(no name)'}")
        shown += 1
        if name_filter and shown >= limit:
            break
    print("-" * 70)
    print(f"Shown: {shown}. Use the id or name to pick a chat.\n")


async def resolve_entity(client, chat):
    """Resolve a chat from: me / numeric id / @username / phone / part of the name."""
    chat = chat.strip()
    if chat.lower() in ("me", "self", "saved"):
        return await client.get_entity("me")

    lookup = chat[1:] if chat.startswith("@") and chat[1:].lstrip("-").isdigit() else chat

    if lookup.lstrip("-").isdigit():
        chat_id = int(lookup)
        try:
            return await client.get_entity(chat_id)
        except Exception:
            async for d in client.iter_dialogs():
                if d.id == chat_id:
                    return d.entity
            raise ValueError(f"No chat found with id {chat_id}.")

    try:
        return await client.get_entity(lookup)
    except Exception:
        pass

    needle = chat.lower()
    matches = []
    async for d in client.iter_dialogs():
        if needle in (d.name or "").lower():
            matches.append(d)
    if len(matches) == 1:
        return matches[0].entity
    if len(matches) > 1:
        names = "\n  ".join(f"{d.name} (id={d.id})" for d in matches[:10])
        raise ValueError(f"More than one chat matches '{chat}':\n  {names}\nUse the exact id.")
    raise ValueError(f"No chat found for '{chat}'.")


def entity_title(entity):
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or " ".join(filter(None, [getattr(entity, "first_name", ""), getattr(entity, "last_name", "")])).strip()
        or "Saved Messages"
    )


async def ask_chat(client, prompt="Enter chat (me for Saved, or @username, or id, or part of the name)"):
    while True:
        raw = ask(prompt)
        if not raw:
            print("  You must type something.")
            continue
        try:
            entity = await resolve_entity(client, raw)
            print(f"  OK chat: {entity_title(entity)}")
            return entity
        except ValueError as e:
            print(f"  {e}")
            if ask_yes_no("  Show the chat list?", default=True):
                await list_dialogs(client, limit=40)


async def collect_message_ids(client, entity, from_dt=None, to_dt=None,
                              only_mine=False, keyword=None):
    """Collect message IDs matching the filters. from_dt/to_dt are optional."""
    ids, dates, scanned = [], [], 0
    offset_date = (to_dt + timedelta(seconds=1)) if to_dt else None

    async for m in client.iter_messages(entity, offset_date=offset_date, search=keyword):
        scanned += 1
        if scanned % 500 == 0:
            print(f"  ... scanned {scanned} messages", end="\r", flush=True)
        if m.date is None:
            continue
        if to_dt and m.date > to_dt:
            continue
        if from_dt and m.date < from_dt:
            break
        if only_mine and not m.out:
            continue
        ids.append(m.id)
        dates.append(m.date)
    print(" " * 50, end="\r")
    return ids, dates, scanned


async def delete_in_batches(client, entity, ids, revoke):
    deleted, batch_size = 0, 100
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        while True:
            try:
                await client.delete_messages(entity, batch, revoke=revoke)
                break
            except FloodWaitError as e:
                wait = e.seconds + 1
                print(f"  Telegram asked to wait {wait}s (anti-spam)...")
                await asyncio.sleep(wait)
        deleted += len(batch)
        print(f"  deleted {deleted}/{len(ids)}", end="\r", flush=True)
    print(" " * 50, end="\r")
    return deleted


async def _preview_and_delete(client, entity, ids, dates, scanned, strong=False):
    """Show the result + confirm + delete. Used by all delete actions."""
    print(f"\nScanned {scanned} messages, found {len(ids)} matching.")
    if ids:
        print(f"  Oldest: {min(dates):%Y-%m-%d %H:%M}   |   Newest: {max(dates):%Y-%m-%d %H:%M}")
    if not confirm_destructive(len(ids), strong=strong):
        print("Cancelled - nothing was deleted.")
        return
    revoke = ask_revoke()
    print(f"\nDeleting {len(ids)} messages ({'for everyone' if revoke else 'your side only'})...")
    n = await delete_in_batches(client, entity, ids, revoke)
    print(f"\nDone - deleted {n} messages.")


# ===========================================================================
# Menu actions
# ===========================================================================
async def menu_delete_range(client):
    print("\n- Delete messages in a date range -")
    entity = await ask_chat(client)
    from_dt = parse_date(ask("From date (e.g. 10/6)"), end_of_day=False)
    to_dt = parse_date(ask("To date (e.g. 16/6)"), end_of_day=True)
    if from_dt > to_dt:
        print("Start date is after end date - cancelled.")
        return
    only_mine = ask_yes_no("Only your own messages?", default=False)
    ids, dates, scanned = await collect_message_ids(client, entity, from_dt, to_dt, only_mine)
    await _preview_and_delete(client, entity, ids, dates, scanned)


async def menu_delete_last_n(client):
    print("\n- Delete the last N messages -")
    entity = await ask_chat(client)
    n = ask_int("How many of the most recent messages?", minimum=1)
    only_mine = ask_yes_no("Only your own messages?", default=False)
    ids, dates, scanned = [], [], 0
    async for m in client.iter_messages(entity, limit=None if only_mine else n):
        scanned += 1
        if only_mine and not m.out:
            continue
        ids.append(m.id)
        if m.date:
            dates.append(m.date)
        if len(ids) >= n:
            break
    await _preview_and_delete(client, entity, ids, dates, scanned)


async def menu_delete_all(client):
    print("\n- Delete ALL messages in a chat -")
    entity = await ask_chat(client)
    ids, dates, scanned = await collect_message_ids(client, entity)
    await _preview_and_delete(client, entity, ids, dates, scanned, strong=True)


async def menu_delete_mine(client):
    print("\n- Delete only my own messages -")
    entity = await ask_chat(client)
    from_dt = to_dt = None
    if ask_yes_no("Limit to a date range? (no = all your messages)", default=False):
        from_dt = parse_date(ask("From date"), end_of_day=False)
        to_dt = parse_date(ask("To date"), end_of_day=True)
    ids, dates, scanned = await collect_message_ids(
        client, entity, from_dt, to_dt, only_mine=True
    )
    await _preview_and_delete(client, entity, ids, dates, scanned)


async def menu_delete_keyword(client):
    print("\n- Delete messages containing a keyword -")
    entity = await ask_chat(client)
    keyword = ask("Enter the keyword")
    if not keyword:
        print("A keyword is required - cancelled.")
        return
    only_mine = ask_yes_no("Only your own messages?", default=False)
    ids, dates, scanned = await collect_message_ids(
        client, entity, only_mine=only_mine, keyword=keyword
    )
    await _preview_and_delete(client, entity, ids, dates, scanned)


def _safe_name(text):
    keep = "".join(c if c.isalnum() or c in " _-" else "_" for c in text)
    return keep.strip().replace(" ", "_")[:50] or "chat"


async def menu_export(client):
    print("\n- Export a chat to a file -")
    entity = await ask_chat(client)
    limit = ask_int("How many recent messages to export? (0 = all)", default=0, minimum=0) or None
    base = f"export_{_safe_name(entity_title(entity))}_{datetime.now().astimezone():%Y%m%d_%H%M%S}"
    json_path, txt_path = base + ".json", base + ".txt"

    records, count = [], 0
    print("Exporting...")
    async for m in client.iter_messages(entity, limit=limit):
        sender = "Me" if m.out else entity_title(entity)
        records.append({
            "id": m.id,
            "date": m.date.isoformat() if m.date else None,
            "out": bool(m.out),
            "sender": sender,
            "text": m.text or "",
            "media": type(m.media).__name__ if m.media else None,
        })
        count += 1
        if count % 200 == 0:
            print(f"  ... {count}", end="\r", flush=True)
    print(" " * 40, end="\r")

    records.reverse()  # oldest first
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"Export: {entity_title(entity)}\nMessages: {count}\n" + "=" * 60 + "\n\n")
        for r in records:
            when = r["date"][:16].replace("T", " ") if r["date"] else "?"
            media = f" [{r['media']}]" if r["media"] else ""
            f.write(f"[{when}] {r['sender']}{media}: {r['text']}\n")

    print(f"\nDone - exported {count} messages:\n  {json_path}\n  {txt_path}")


async def menu_download_media(client):
    print("\n- Download media from a chat -")
    entity = await ask_chat(client)
    limit = ask_int("How many recent messages to scan? (0 = all)", default=0, minimum=0) or None
    folder = f"media_{_safe_name(entity_title(entity))}"
    os.makedirs(folder, exist_ok=True)
    downloaded, scanned = 0, 0
    print(f"Downloading into folder: {folder}")
    async for m in client.iter_messages(entity, limit=limit):
        scanned += 1
        if m.media:
            try:
                path = await m.download_media(file=folder + os.sep)
                if path:
                    downloaded += 1
                    print(f"  ({downloaded}) {os.path.basename(path)}")
            except Exception as e:
                print(f"  Failed to download message {m.id}: {e}")
    print(f"\nDone - downloaded {downloaded} files from {scanned} messages into: {folder}")


async def menu_search(client):
    print("\n- Search messages by keyword -")
    entity = await ask_chat(client)
    keyword = ask("Enter the keyword")
    if not keyword:
        print("A keyword is required - cancelled.")
        return
    limit = ask_int("Max results to show?", default=30, minimum=1)
    print("-" * 70)
    n = 0
    async for m in client.iter_messages(entity, search=keyword, limit=limit):
        n += 1
        who = "Me" if m.out else entity_title(entity)
        when = f"{m.date:%Y-%m-%d %H:%M}" if m.date else "?"
        text = (m.text or "").replace("\n", " ")
        print(f"  [{when}] {who}: {text[:120]}")
    print("-" * 70)
    print(f"Results shown: {n}\n")


async def menu_list_chats(client):
    print("\n- List / search chats -")
    name = ask("Filter by name (leave empty for most recent)", default="")
    limit = ask_int("How many chats to show?", default=40, minimum=1)
    await list_dialogs(client, limit=limit, name_filter=name or None)


async def menu_stats(client):
    print("\n- Chat statistics -")
    entity = await ask_chat(client)
    total = (await client.get_messages(entity, limit=0)).total
    newest = await client.get_messages(entity, limit=1)
    oldest = await client.get_messages(entity, limit=1, reverse=True)
    print("-" * 50)
    print(f"  Chat: {entity_title(entity)}")
    print(f"  Total messages: {total}")
    if newest:
        print(f"  Newest message: {newest[0].date:%Y-%m-%d %H:%M}")
    if oldest:
        print(f"  Oldest message: {oldest[0].date:%Y-%m-%d %H:%M}")
    print("-" * 50 + "\n")


async def menu_account_info(client):
    print("\n- My account info -")
    me = await client.get_me()
    print("-" * 50)
    print(f"  Name:      {((me.first_name or '') + ' ' + (me.last_name or '')).strip()}")
    print(f"  Username:  @{me.username}" if me.username else "  Username:  (none)")
    print(f"  Phone:     {me.phone}")
    print(f"  ID:        {me.id}")
    print("-" * 50 + "\n")


async def menu_send(client):
    print("\n- Send a message -")
    entity = await ask_chat(client)
    text = ask("Type the message text")
    if not text:
        print("Empty message - cancelled.")
        return
    schedule = None
    if ask_yes_no("Schedule it for later?", default=False):
        schedule = parse_datetime(ask("When? (e.g. 2026-06-20 14:30)"))
    await client.send_message(entity, text, schedule=schedule)
    print("Sent." if not schedule else f"Scheduled for {schedule:%Y-%m-%d %H:%M}.")


async def menu_forward(client):
    print("\n- Forward / save messages -")
    print("Source (the chat to take messages from):")
    source = await ask_chat(client)
    print("Destination (me for Saved, or another chat):")
    target = await ask_chat(client)
    n = ask_int("Forward how many recent messages from the source?", minimum=1)
    msgs = await client.get_messages(source, limit=n)
    if not msgs:
        print("No messages - cancelled.")
        return
    await client.forward_messages(target, list(reversed(msgs)), source)
    print(f"Forwarded {len(msgs)} messages to {entity_title(target)}.")


async def menu_schedule(client):
    print("\n- Scheduled auto-cleanup (Windows Task Scheduler) -")
    if sys.platform != "win32":
        print("This feature is Windows-only (it uses Task Scheduler).")
        return

    # Manage / remove an existing task
    if ask_yes_no("Remove an existing scheduled cleanup instead of creating one?",
                  default=False):
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip() or "Done.")
        return

    print("This creates a task that auto-deletes OLD messages on a schedule.")
    entity = await ask_chat(client)
    me = await client.get_me()
    token = "me" if getattr(entity, "id", None) == me.id else str(entity.id)

    days = ask_int("Delete messages older than how many days?", default=7, minimum=1)
    only_mine = ask_yes_no("Only your own messages?", default=False)
    revoke = ask_revoke()
    freq = ask("Frequency: daily or weekly? (d/w)", "d").lower()
    sched = "WEEKLY" if freq.startswith("w") else "DAILY"
    day = ask("Day of week (MON,TUE,WED,THU,FRI,SAT,SUN)", "SUN").upper() if sched == "WEEKLY" else None
    time_str = ask("Time of day HH:MM (24h)", "03:00")

    extra = (" --only-mine" if only_mine else "") + ("" if revoke else " --no-revoke")
    bat_path = os.path.join(SCRIPT_DIR, "auto_cleanup.bat")
    script_path = os.path.join(SCRIPT_DIR, "telegram_tool.py")
    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\n"
            "chcp 65001 >NUL\r\n"
            'cd /d "%~dp0"\r\n'
            f'"{sys.executable}" "{script_path}" --auto --chat {token} '
            f"--older-than {days} --yes{extra}\r\n"
        )

    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", bat_path,
           "/SC", sched, "/ST", time_str, "/F"]
    if day:
        cmd += ["/D", day]
    r = subprocess.run(cmd, capture_output=True, text=True)

    if r.returncode == 0:
        when = f"every {day}" if day else "every day"
        print(f"\nScheduled OK. Task '{TASK_NAME}' runs {when} at {time_str}.")
        print(f"It deletes messages older than {days} days in '{entity_title(entity)}' "
              f"({'for everyone' if revoke else 'your side only'}).")
        print("Note: it runs while you are logged into Windows.")
        print("To remove it later, run this menu option again and choose remove.")
    else:
        print("Failed to create the scheduled task:")
        print(r.stdout.strip())
        print(r.stderr.strip())


MENU = [
    ("DELETE TOOLS", None),
    ("1", "Delete messages in a date range", menu_delete_range),
    ("2", "Delete the last N messages", menu_delete_last_n),
    ("3", "Delete ALL messages in a chat", menu_delete_all),
    ("4", "Delete only my own messages", menu_delete_mine),
    ("5", "Delete messages containing a keyword", menu_delete_keyword),
    ("BACKUP / EXPORT", None),
    ("6", "Export a chat to a file (JSON + text)", menu_export),
    ("7", "Download media from a chat", menu_download_media),
    ("SEARCH / BROWSE", None),
    ("8", "Search messages by keyword", menu_search),
    ("9", "List / search chats", menu_list_chats),
    ("10", "Chat statistics", menu_stats),
    ("11", "My account info", menu_account_info),
    ("SEND / FORWARD", None),
    ("12", "Send a message (now or scheduled)", menu_send),
    ("13", "Forward / save messages", menu_forward),
    ("AUTOMATION", None),
    ("14", "Set up scheduled auto-cleanup (Windows)", menu_schedule),
]
ACTIONS = {item[0]: item[2] for item in MENU if item[1] is not None}


def print_menu():
    print("\n" + "=" * 50)
    print("        Telegram Toolbox")
    print("=" * 50)
    for item in MENU:
        if item[1] is None:  # section header
            print(f"\n  {item[0]}")
        else:
            key, label, _ = item
            print(f"   {key:>2}) {label}")
    print("\n    0) Exit")
    print("=" * 50)


async def main_loop(client):
    me = await client.get_me()
    print(f"\nWelcome {me.first_name or ''}  (login OK)")
    while True:
        print_menu()
        choice = ask("Choose a number").strip()
        if choice in ("0", "q", "exit", "quit"):
            print("Bye.")
            return
        action = ACTIONS.get(choice)
        if not action:
            print("Invalid choice - try again.")
            continue
        try:
            await action(client)
        except KeyboardInterrupt:
            print("\nAction cancelled, back to menu.")
        except Exception as e:
            print(f"\nError: {e}")
        ask("\n(Press Enter to return to the menu)")


async def run_auto(client, args):
    """Non-interactive delete, for scheduled/automated runs."""
    if not args.chat:
        sys.exit("--auto requires --chat.")
    entity = await resolve_entity(client, args.chat)
    now = datetime.now().astimezone()
    from_dt = to_dt = None
    if args.older_than is not None:
        to_dt = now - timedelta(days=args.older_than)          # older than N days
    elif args.last_days is not None:
        from_dt = now - timedelta(days=args.last_days)          # within last N days
    elif args.from_date and args.to_date:
        from_dt = parse_date(args.from_date, end_of_day=False)
        to_dt = parse_date(args.to_date, end_of_day=True)
    else:
        sys.exit("Specify --older-than N, or --last-days N, or both --from and --to.")

    print(f"[{now:%Y-%m-%d %H:%M}] auto-clean | chat={entity_title(entity)} | "
          f"only_mine={args.only_mine}")
    ids, dates, scanned = await collect_message_ids(
        client, entity, from_dt, to_dt, only_mine=args.only_mine)
    print(f"Scanned {scanned}, matched {len(ids)}.")
    if not ids:
        return
    if not args.yes:
        print("DRY RUN (no --yes) - nothing deleted.")
        return
    revoke = not args.no_revoke
    n = await delete_in_batches(client, entity, ids, revoke)
    print(f"Deleted {n} messages ({'for everyone' if revoke else 'your side only'}).")


def build_parser():
    p = argparse.ArgumentParser(
        description="Telegram Toolbox. Run with no arguments for the interactive menu, "
                    "or use --auto for a non-interactive scheduled cleanup.")
    p.add_argument("--auto", action="store_true",
                   help="Run a non-interactive automated delete (for scheduling).")
    p.add_argument("--chat", help="me / @username / numeric id / part of the name")
    p.add_argument("--older-than", type=int, dest="older_than",
                   help="Delete messages older than N days.")
    p.add_argument("--last-days", type=int, dest="last_days",
                   help="Delete messages from the last N days.")
    p.add_argument("--from", dest="from_date", help="Range start date (with --to).")
    p.add_argument("--to", dest="to_date", help="Range end date (with --from).")
    p.add_argument("--only-mine", action="store_true", dest="only_mine",
                   help="Only your own messages.")
    p.add_argument("--no-revoke", action="store_true", dest="no_revoke",
                   help="Delete on your side only (default deletes for everyone).")
    p.add_argument("--yes", action="store_true",
                   help="Actually delete. Without it, --auto only previews (dry run).")
    return p


async def run(args):
    load_env()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    phone = os.environ.get("TG_PHONE")
    if not api_id or not api_hash:
        sys.exit(
            "Missing settings. Create a .env file (copy from .env.example) with:\n"
            "    TG_API_ID=...\n    TG_API_HASH=...\n    TG_PHONE=+20...\n"
            "Get api_id and api_hash from https://my.telegram.org"
        )
    session_name = os.environ.get("TG_SESSION", "telegram_session")
    if not os.path.isabs(session_name):
        session_name = os.path.join(SCRIPT_DIR, session_name)
    client = TelegramClient(session_name, int(api_id), api_hash)
    await client.start(phone=phone)
    try:
        if args.auto or args.chat:
            await run_auto(client, args)
        else:
            await main_loop(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parsed_args = build_parser().parse_args()
    try:
        asyncio.run(run(parsed_args))
    except KeyboardInterrupt:
        print("\nCancelled.")
