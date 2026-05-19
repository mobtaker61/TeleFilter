"""
ابزار کمکی برای پیدا کردن Chat ID و Topic ID
اجرا کن: python helper.py
"""
# -*- coding: utf-8 -*-
import asyncio
import json
from telethon import TelegramClient

try:
    from telethon.tl.functions.messages import GetForumTopicsRequest
    HAS_FORUM_API = True
except ImportError:
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest
        HAS_FORUM_API = True
    except ImportError:
        HAS_FORUM_API = False

with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

client = TelegramClient('telefilter_session', config['api_id'], config['api_hash'])


async def list_dialogs():
    print("\n" + "=" * 65)
    print(f"{'نام چت':<38} {'ID'}")
    print("=" * 65)
    async for dialog in client.iter_dialogs(limit=150):
        print(f"{dialog.name:<38} {dialog.id}")


async def list_topics():
    group_id = config.get('target_group_id')
    if not group_id:
        group_id = int(input("ID گروه هدف را وارد کن: ").strip())

    if not HAS_FORUM_API:
        print("\n[!] نسخه Telethon شما GetForumTopicsRequest را ندارد.")
        print("    Topic ID را از این روش پیدا کن:")
        print("    1. در تلگرام دسکتاپ، روی یک Topic کلیک راست کن")
        print("    2. گزینه 'Copy Link' را بزن")
        print("    3. عدد آخر لینک = Topic ID است")
        print("       مثال: t.me/c/1234567890/5  →  Topic ID = 5")
        return

    try:
        result = await client(GetForumTopicsRequest(
            channel=group_id,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q=''
        ))
        print("\n" + "=" * 65)
        print(f"{'عنوان Topic':<45} {'ID'}")
        print("=" * 65)
        for topic in result.topics:
            print(f"{topic.title:<45} {topic.id}")
    except Exception as e:
        print(f"\nخطا: {e}")
        print("مطمئن شو گروه Forum/Topics فعال است.")


async def main():
    await client.start()
    print("\nچه کاری می‌خواهی انجام دهی؟")
    print("  1. لیست همه چت‌ها و کانال‌ها  (پیدا کردن Chat ID)")
    print("  2. لیست Topic های گروه هدف    (پیدا کردن Topic ID)")
    choice = input("\nعدد را وارد کن (1 یا 2): ").strip()

    if choice == '1':
        await list_dialogs()
    elif choice == '2':
        await list_topics()
    else:
        print("انتخاب نامعتبر")


asyncio.run(main())
