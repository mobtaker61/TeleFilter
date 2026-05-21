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

# source_map: {peer_id: [(group_gid, topic_id, filters, is_forum), ...]}
source_map: dict[int, list] = {}
target_entities: dict[str, object] = {}
target_forum: dict[str, bool] = {}


def _peer_keys(entity) -> set[int]:
    """شناسه‌های ممکن یک چت برای تطبیق با event.chat_id."""
    keys = set()
    try:
        keys.add(get_peer_id(entity))
    except Exception:
        pass
    cid = getattr(entity, 'id', None)
    if cid is None:
        return keys
    cid = int(cid)
    keys.add(cid)
    keys.add(-cid)
    if cid > 0:
        keys.add(int(f'-100{cid}'))
    return keys


def _add_source_route(peer_keys: set[int], route: tuple):
    for key in peer_keys:
        if key not in source_map:
            source_map[key] = []
        source_map[key].append(route)


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
        f"    ► مطمئن شو اکانت تلگرام عضو این گروه/کانال است.\n"
        f"    ► یا از @username به جای ID استفاده کن."
    )


def _group_is_forum(group: dict, entity) -> bool:
    if 'is_forum' in group and group['is_forum'] is not None:
        return bool(group['is_forum'])
    return bool(getattr(entity, 'forum', False))


async def setup():
    global target_entities, source_map, target_forum
    source_map = {}
    target_entities = {}
    target_forum = {}

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
            is_forum = _group_is_forum(group, entity)
            target_forum[gid] = is_forum
            logger.info(f"Target: {getattr(entity, 'title', title)} ({'forum' if is_forum else 'عادی/کانال'})")
        except Exception as e:
            logger.error(f"  [گروه {title}] resolve failed: {e}")
            continue

        topics = group.get('topics') or []
        if not topics and not is_forum:
            topics = [{'topic_id': 0, 'name': 'چت اصلی', 'sources': []}]

        for topic in topics:
            topic_id = topic.get('topic_id')
            if topic_id is None:
                continue
            topic_id = int(topic_id)
            topic_name = topic.get('name', str(topic_id))
            use_topic = is_forum

            for source in topic.get('sources') or []:
                chat = source.get('chat', '')
                filters = source.get('filters') or []
                if not str(chat).strip():
                    continue

                try:
                    chat_val = chat
                    try:
                        chat_val = int(chat)
                    except (ValueError, TypeError):
                        pass

                    entity = await _resolve_entity(client, chat_val)
                    peer_keys = _peer_keys(entity)
                    name = getattr(entity, 'title', None) or getattr(entity, 'username', None) or getattr(entity, 'first_name', None) or str(next(iter(peer_keys), chat))

                    route = (gid, topic_id, filters, use_topic)
                    _add_source_route(peer_keys, route)

                    dest = f"{title} / {topic_name}" if use_topic else title
                    filter_info = f"{len(filters)} فیلتر" if filters else "همه پیام‌ها"
                    logger.info(f"  [{dest}] ← {name}  ({filter_info})")
                except ValueError:
                    logger.warning(
                        f"  [{topic_name}] '{chat}' پیدا نشد.\n"
                        f"    ► اکانت باید عضو سورس باشد یا از @username استفاده کن."
                    )
                except Exception as e:
                    logger.error(f"  [{topic_name}] Could not resolve '{chat}': {e}")

    logger.info(f"Active routes: {sum(len(v) for v in source_map.values())}")


def _matches_filters(text: str, filters: list) -> bool:
    if not filters:
        return True
    for rule in filters:
        if isinstance(rule, str):
            rule = [rule]
        if all(phrase.lower() in text for phrase in rule):
            return True
    return False


async def _forward_to_target(event, group_gid, topic_id, use_topic):
    target = target_entities.get(group_gid)
    if not target:
        return
    kwargs = {
        'from_peer': event.chat_id,
        'id': [event.message.id],
        'to_peer': target,
        'random_id': [random.randint(1, 2**63)],
    }
    if use_topic:
        kwargs['top_msg_id'] = topic_id
    await client(ForwardMessagesRequest(**kwargs))


@client.on(events.NewMessage())
async def forward_handler(event):
    if event.out:
        return

    chat = await event.get_chat()
    peer_id = get_peer_id(chat)
    lookup_ids = {peer_id, event.chat_id}
    if hasattr(event, 'peer_id') and event.peer_id:
        try:
            lookup_ids.add(get_peer_id(event.peer_id))
        except Exception:
            pass
    entries = None
    for lid in lookup_ids:
        entries = source_map.get(lid)
        if entries:
            break
    if not entries:
        return

    text = (event.message.text or event.message.message or '').lower()

    for group_gid, topic_id, filters, use_topic in entries:
        if not _matches_filters(text, filters):
            continue
        try:
            await _forward_to_target(event, group_gid, topic_id, use_topic)
            dest = f"group={group_gid}" + (f" topic={topic_id}" if use_topic else "")
            logger.info(f"Forwarded chat={peer_id} → {dest}")
            record_forward(args.user_id, group_gid, topic_id if use_topic else 0, peer_id)
        except Exception as e:
            logger.error(f"Forward failed (chat={peer_id}, group={group_gid}, topic={topic_id}): {e}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        logger.error(
            "Session not authorized. Complete phone/QR login in the panel once."
        )
        raise SystemExit(1)
    await setup()
    if not source_map:
        logger.warning(
            "No sources configured — add sources in panel, then SAVE (ذخیره تغییرات)."
        )
    logger.info("TeleFilter is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
