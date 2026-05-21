import asyncio
import json
import random
import logging
import argparse
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.utils import get_peer_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument('--config',  default='config.json',         help='path to user config.json')
parser.add_argument('--session', default='telefilter_session',  help='path to telethon session file')
args = parser.parse_args()

with open(args.config, 'r', encoding='utf-8') as f:
    config = json.load(f)

client = TelegramClient(args.session, config['api_id'], config['api_hash'])

# source_map: {peer_id: [(topic_id, [filter_phrases])]}
# یک سورس می‌تواند در چند topic مختلف با فیلترهای متفاوت باشد
source_map: dict[int, list] = {}
target_entity = None


async def _resolve_entity(client, identifier):
    """
    Entity را پیدا می‌کند.
    اگر get_entity مستقیم ناموفق بود (entity در cache نیست)،
    از طریق iter_dialogs جستجو می‌کند تا cache پر شود.
    """
    try:
        return await client.get_entity(identifier)
    except (ValueError, KeyError):
        pass

    # entity در cache نیست — از dialogs جستجو کن
    logger.info(f"Entity '{identifier}' not cached — scanning dialogs…")
    target_id = None
    try:
        raw = int(str(identifier).strip())
        # تلگرام ID سوپرگروه/کانال را با پیشوند -100 ذخیره می‌کند
        # هر دو فرمت را چک می‌کنیم
        candidates = {raw, abs(raw)}
        str_raw = str(abs(raw))
        if str_raw.startswith('100'):
            candidates.add(int(str_raw[3:]))
        target_id = candidates
    except (ValueError, TypeError):
        pass

    async for dialog in client.iter_dialogs():
        did = dialog.id
        if target_id and did in target_id:
            logger.info(f"Found via dialogs ✓")
            return dialog.entity
        if not target_id and str(identifier) in (dialog.name or '', getattr(dialog.entity, 'username', '') or ''):
            return dialog.entity

    raise ValueError(
        f"Cannot find entity '{identifier}'.\n"
        f"    ► مطمئن شو اکانت تلگرام عضو این گروه است.\n"
        f"    ► یا از @username گروه به جای ID استفاده کن."
    )


async def setup():
    global target_entity
    target_entity = await _resolve_entity(client, config['target_group_id'])
    logger.info(f"Target group: {getattr(target_entity, 'title', target_entity.id)}")

    for topic in config.get('topics', []):
        topic_id = topic['topic_id']
        topic_name = topic.get('name', str(topic_id))

        for source in topic.get('sources', []):
            chat = source.get('chat', '')
            filters = source.get('filters', [])  # لیست عبارات فیلتر

            try:
                chat_val = chat
                try:
                    chat_val = int(chat)
                except (ValueError, TypeError):
                    pass  # string/username → همانطور بماند

                entity = await _resolve_entity(client, chat_val)
                peer_id = get_peer_id(entity)
                name = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(peer_id)

                if peer_id not in source_map:
                    source_map[peer_id] = []
                source_map[peer_id].append((topic_id, filters))

                filter_info = f"{len(filters)} فیلتر" if filters else "همه پیام‌ها"
                logger.info(f"  [{topic_name}] ← {name}  ({filter_info})")
            except ValueError:
                logger.warning(
                    f"  [{topic_name}] '{chat}' پیدا نشد.\n"
                    f"    ► اگر چت خصوصی است، ابتدا یک پیام به او بفرست یا\n"
                    f"      بجای User ID عددی از @username استفاده کن."
                )
            except Exception as e:
                logger.error(f"  [{topic_name}] Could not resolve '{chat}': {e}")


def _matches_filters(text: str, filters: list) -> bool:
    """
    filters می‌تواند دو فرمت داشته باشد:
      • قدیمی (backward-compat): ["عبارت۱", "عبارت۲"]
        → هر کدام باشد (OR)
      • جدید: [["عبارت۱", "عبارت۲"], ["عبارت۳"]]
        → هر rule یک AND-group است؛ بین rule‌ها OR است
    """
    if not filters:
        return True   # بدون فیلتر = همه پیام‌ها
    for rule in filters:
        if isinstance(rule, str):
            rule = [rule]          # backward-compat
        if all(phrase.lower() in text for phrase in rule):
            return True            # این rule match شد → فوروارد
    return False


@client.on(events.NewMessage())
async def forward_handler(event):
    entries = source_map.get(event.chat_id)
    if not entries:
        return

    text = (event.message.text or event.message.message or '').lower()

    for topic_id, filters in entries:
        if not _matches_filters(text, filters):
            continue

        try:
            await client(ForwardMessagesRequest(
                from_peer=event.chat_id,
                id=[event.message.id],
                to_peer=target_entity,
                top_msg_id=topic_id,
                random_id=[random.randint(1, 2**63)]
            ))
            logger.info(f"Forwarded  chat={event.chat_id}  →  topic={topic_id}")
        except Exception as e:
            logger.error(f"Forward failed (chat={event.chat_id}, topic={topic_id}): {e}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(
            "Session not authorized. Log in via the panel "
            "(Telegram phone code), then restart."
        )
        raise SystemExit(1)
    await setup()
    logger.info("TeleFilter is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
