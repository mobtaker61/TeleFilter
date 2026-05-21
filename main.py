import asyncio
import json
import random
import logging
import argparse
import os
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.utils import get_peer_id

from config_util import normalize_config, record_forward

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument('--config',  default='config.json', help='path to user config.json')
parser.add_argument('--session', default='telefilter_session', help='telethon session path (no .session suffix)')
parser.add_argument('--user-id', type=int, default=0, help='panel user id for stats')
args = parser.parse_args()

with open(args.config, 'r', encoding='utf-8') as f:
    config = normalize_config(json.load(f))

client = TelegramClient(args.session, config['api_id'], config['api_hash'])

# source_map: {peer_id: [(target_entity, topic_id, filters, group_gid), ...]}
source_map: dict[int, list] = {}
target_entities: dict[str, object] = {}


async def _resolve_entity(client, identifier):
    try:
        return await client.get_entity(identifier)
    except (ValueError, KeyError):
        pass

    logger.info(f"Entity '{identifier}' not cached — scanning dialogs…")
    target_id = None
    try:
        raw = int(str(identifier).strip())
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
            logger.info("Found via dialogs ✓")
            return dialog.entity
        if not target_id and str(identifier) in (dialog.name or '', getattr(dialog.entity, 'username', '') or ''):
            return dialog.entity

    raise ValueError(
        f"Cannot find entity '{identifier}'.\n"
        f"    ► مطمئن شو اکانت تلگرام عضو این گروه است.\n"
        f"    ► یا از @username گروه به جای ID استفاده کن."
    )


async def setup():
    global target_entities, source_map
    source_map = {}
    target_entities = {}

    for group in config.get('groups') or []:
        gid = group.get('id', '')
        tg_id = group.get('telegram_id', '')
        title = group.get('title', tg_id)
        if not tg_id:
            logger.warning(f"  [گروه {title}] telegram_id خالی است — رد شد")
            continue

        try:
            entity = await _resolve_entity(client, int(tg_id))
            target_entities[gid] = entity
            logger.info(f"Target group: {getattr(entity, 'title', title)}")
        except Exception as e:
            logger.error(f"  [گروه {title}] resolve failed: {e}")
            continue

        for topic in group.get('topics') or []:
            topic_id = topic['topic_id']
            topic_name = topic.get('name', str(topic_id))

            for source in topic.get('sources') or []:
                chat = source.get('chat', '')
                filters = source.get('filters', [])

                try:
                    chat_val = chat
                    try:
                        chat_val = int(chat)
                    except (ValueError, TypeError):
                        pass

                    entity = await _resolve_entity(client, chat_val)
                    peer_id = get_peer_id(entity)
                    name = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(peer_id)

                    if peer_id not in source_map:
                        source_map[peer_id] = []
                    source_map[peer_id].append((gid, topic_id, filters))

                    filter_info = f"{len(filters)} فیلتر" if filters else "همه پیام‌ها"
                    logger.info(f"  [{title} / {topic_name}] ← {name}  ({filter_info})")
                except ValueError:
                    logger.warning(
                        f"  [{topic_name}] '{chat}' پیدا نشد.\n"
                        f"    ► اگر چت خصوصی است، ابتدا یک پیام به او بفرست یا\n"
                        f"      بجای User ID عددی از @username استفاده کن."
                    )
                except Exception as e:
                    logger.error(f"  [{topic_name}] Could not resolve '{chat}': {e}")


def _matches_filters(text: str, filters: list) -> bool:
    if not filters:
        return True
    for rule in filters:
        if isinstance(rule, str):
            rule = [rule]
        if all(phrase.lower() in text for phrase in rule):
            return True
    return False


@client.on(events.NewMessage())
async def forward_handler(event):
    entries = source_map.get(event.chat_id)
    if not entries:
        return

    text = (event.message.text or event.message.message or '').lower()

    for group_gid, topic_id, filters in entries:
        if not _matches_filters(text, filters):
            continue

        target = target_entities.get(group_gid)
        if not target:
            continue

        try:
            await client(ForwardMessagesRequest(
                from_peer=event.chat_id,
                id=[event.message.id],
                to_peer=target,
                top_msg_id=topic_id,
                random_id=[random.randint(1, 2**63)]
            ))
            logger.info(f"Forwarded  chat={event.chat_id}  →  group={group_gid} topic={topic_id}")
            record_forward(args.user_id, group_gid, topic_id, event.chat_id)
        except Exception as e:
            logger.error(f"Forward failed (chat={event.chat_id}, topic={topic_id}): {e}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(
            "Session not authorized. Complete phone login in the panel once."
        )
        raise SystemExit(1)
    await setup()
    if not source_map:
        logger.warning("No sources configured — waiting for messages anyway.")
    logger.info("TeleFilter is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
