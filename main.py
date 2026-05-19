import asyncio
import json
import random
import logging
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.utils import get_peer_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

client = TelegramClient('telefilter_session', config['api_id'], config['api_hash'])

# {chat_id: source_config_dict}
source_map: dict[int, dict] = {}
target_entity = None


def find_topic(text: str, source_cfg: dict) -> int | None:
    """
    فیلترهای کلمه کلیدی را بررسی می‌کند.
    اگر مطابقت پیدا شد topic_id مربوط را برمی‌گرداند.
    اگر نه، topic_id پیش‌فرض منبع را برمی‌گرداند (یا None).
    """
    text_lower = (text or '').lower()
    for f in source_cfg.get('filters', []):
        if any(kw.lower() in text_lower for kw in f['keywords']):
            return f['topic_id']
    return source_cfg.get('topic_id')


async def setup():
    global target_entity
    target_entity = await client.get_entity(config['target_group_id'])
    logger.info(f"Target group: {getattr(target_entity, 'title', target_entity.id)}")

    for source in config['sources']:
        try:
            entity = await client.get_entity(source['chat'])
            peer_id = get_peer_id(entity)
            source_map[peer_id] = source
            name = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(peer_id)
            filters = source.get('filters', [])
            default_topic = source.get('topic_id', '—')
            logger.info(f"  Monitoring: {name}  |  default topic={default_topic}  |  keyword filters={len(filters)}")
        except Exception as e:
            logger.error(f"  Could not resolve '{source['chat']}': {e}")


@client.on(events.NewMessage())
async def forward_handler(event):
    source_cfg = source_map.get(event.chat_id)
    if source_cfg is None:
        return

    text = event.message.text or event.message.message or ''
    topic_id = find_topic(text, source_cfg)

    if topic_id is None:
        return  # هیچ فیلتری مطابقت نداشت و topic پیش‌فرض هم تعریف نشده

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
    await client.start()
    await setup()
    logger.info("TeleFilter is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
