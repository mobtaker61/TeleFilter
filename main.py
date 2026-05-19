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

# source_map: {peer_id: [(topic_id, [filter_phrases])]}
# یک سورس می‌تواند در چند topic مختلف با فیلترهای متفاوت باشد
source_map: dict[int, list] = {}
target_entity = None


async def setup():
    global target_entity
    target_entity = await client.get_entity(config['target_group_id'])
    logger.info(f"Target group: {getattr(target_entity, 'title', target_entity.id)}")

    for topic in config.get('topics', []):
        topic_id = topic['topic_id']
        topic_name = topic.get('name', str(topic_id))

        for source in topic.get('sources', []):
            chat = source.get('chat', '')
            filters = source.get('filters', [])  # لیست عبارات فیلتر

            try:
                # اگر chat یک عدد خالص باشد، ابتدا سعی می‌کنیم
                # با get_input_entity که از cache استفاده می‌کند resolve کنیم
                chat_val = chat
                try:
                    chat_val = int(chat)
                except (ValueError, TypeError):
                    pass  # string/username → همانطور بماند

                entity = await client.get_entity(chat_val)
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


@client.on(events.NewMessage())
async def forward_handler(event):
    entries = source_map.get(event.chat_id)
    if not entries:
        return

    text = (event.message.text or event.message.message or '').lower()

    for topic_id, filters in entries:
        # اگر فیلتری نداشت → همه پیام‌ها فوروارد شوند
        # اگر فیلتر داشت → فقط اگر حداقل یک عبارت در پیام پیدا شود
        if filters and not any(f.lower() in text for f in filters):
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
    await client.start()
    await setup()
    logger.info("TeleFilter is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
