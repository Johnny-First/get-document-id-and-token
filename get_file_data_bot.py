#!/usr/bin/env python3
"""
Temporary MAX bot for inspecting incoming file/audio/document attachments.

The bot uses polling, does not touch any project database or external services,
and only prints/returns technical data from received MAX messages.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
import sys
from typing import Any, Callable

try:
    from dotenv import load_dotenv
    import maxapi
    from maxapi import Bot, Dispatcher
    from maxapi.types import Command
except ImportError:
    print("Установите зависимость: pip install maxapi python-dotenv", file=sys.stderr)
    sys.exit(1)


TARGET_KEYS = {
    "id",
    "file_id",
    "attachment_id",
    "token",
    "type",
    "payload",
    "url",
    "file",
    "media",
    "attachments",
    "filename",
    "name",
    "size",
    "mime_type",
}

MESSAGE_PART_LIMIT = 3000
SKIP_SERIALIZED_KEYS = {"bot", "client", "session", "http", "api"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("max-file-data-bot")


def object_to_plain_data(obj: Any, _seen: set[int] | None = None) -> Any:
    """
    Рекурсивно превращает объект maxapi/pydantic/dataclass/list/dict в обычный dict/list/str/int.
    Должна поддерживать:
    - dict
    - list/tuple
    - pydantic model_dump()
    - dataclass asdict()
    - объект с __dict__
    - обычные значения
    """
    if _seen is None:
        _seen = set()

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    obj_id = id(obj)
    if obj_id in _seen:
        return "<recursive>"
    _seen.add(obj_id)

    if isinstance(obj, dict):
        return {
            str(key): object_to_plain_data(value, _seen)
            for key, value in obj.items()
            if str(key) not in SKIP_SERIALIZED_KEYS
        }

    if isinstance(obj, (list, tuple, set)):
        return [object_to_plain_data(item, _seen) for item in obj]

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return object_to_plain_data(dataclasses.asdict(obj), _seen)
        except Exception:
            logger.exception("Failed to convert dataclass with asdict")

    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(exclude=SKIP_SERIALIZED_KEYS)
            return object_to_plain_data(dumped, _seen)
        except Exception:
            logger.exception("Failed to convert pydantic object with model_dump")

    dict_method = getattr(obj, "dict", None)
    if callable(dict_method):
        try:
            return object_to_plain_data(dict_method(), _seen)
        except Exception:
            logger.exception("Failed to convert object with dict()")

    obj_dict = getattr(obj, "__dict__", None)
    if isinstance(obj_dict, dict):
        return {
            str(key): object_to_plain_data(value, _seen)
            for key, value in obj_dict.items()
            if not key.startswith("_") and key not in SKIP_SERIALIZED_KEYS
        }

    return str(obj)


def find_keys_recursive(data: Any, target_keys: set[str]) -> dict[str, list[Any]]:
    """
    Рекурсивно ищет все target_keys в data.
    Возвращает dict:
    {
      "token": [...],
      "file_id": [...],
      "id": [...],
      ...
    }
    """
    found: dict[str, list[Any]] = {key: [] for key in sorted(target_keys)}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_str = str(key)
                if key_str in target_keys:
                    found[key_str].append(child)
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found


def safe_json_dumps(data: Any) -> str:
    """
    json.dumps с ensure_ascii=False, indent=2, default=str.
    """
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def mask_token(token: Any) -> str:
    """
    В обычном логе маскирует token, но в ответ пользователю показывает полный token,
    потому что задача бота - получить его.
    """
    token_str = str(token)
    if len(token_str) <= 8:
        return "***"
    return f"{token_str[:4]}...{token_str[-4:]}"


def parse_admin_ids(value: str | None) -> set[int]:
    if not value:
        return set()

    admin_ids: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            admin_ids.add(int(item))
        except ValueError:
            logger.warning("Ignoring invalid ADMIN_IDS value: %s", item)
    return admin_ids


def first_values(found: dict[str, list[Any]], key: str) -> list[Any]:
    return found.get(key, [])


def format_values(title: str, values: list[Any]) -> str:
    if not values:
        return f"{title}:\nне найдено"
    if len(values) == 1:
        return f"{title}:\n{safe_json_dumps(values[0]) if isinstance(values[0], (dict, list)) else values[0]}"

    lines = [f"{title}:"]
    for index, value in enumerate(values, start=1):
        formatted = safe_json_dumps(value) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"{index}) {formatted}")
    return "\n".join(lines)


def looks_like_attachment_data(data: Any) -> bool:
    if isinstance(data, dict):
        for key in ("attachments", "attachment", "media", "file", "audio", "document", "photo", "video"):
            value = data.get(key)
            if value:
                return True
        return any(looks_like_attachment_data(value) for value in data.values())

    if isinstance(data, list):
        return any(looks_like_attachment_data(item) for item in data)

    return False


def get_user_id(data: Any) -> Any:
    if not isinstance(data, dict):
        return None

    candidate_paths = (
        ("sender", "user_id"),
        ("sender", "id"),
        ("from", "user_id"),
        ("from", "id"),
        ("user", "id"),
        ("author", "id"),
        ("recipient", "user_id"),
        ("chat", "id"),
    )
    for path in candidate_paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return current

    for key in ("user_id", "from_id", "sender_id"):
        if key in data:
            return data[key]

    return None


def get_message_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    for key in ("text", "body", "message", "content"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            nested_text = get_message_text(value)
            if nested_text:
                return nested_text
    return ""


def extract_user_message_object(event_or_message: Any) -> Any:
    """
    maxapi passes MessageCreated events to handlers. The useful raw payload is
    usually in event.message; this avoids serializing service objects like bot.
    """
    event_message = getattr(event_or_message, "message", None)
    if event_message is not None:
        return event_message
    return event_or_message


def summarize_found_for_log(found: dict[str, list[Any]]) -> dict[str, list[Any]]:
    summary: dict[str, list[Any]] = {}
    for key, values in found.items():
        if not values:
            continue
        if key == "token":
            summary[key] = [mask_token(value) for value in values]
        else:
            summary[key] = values
    return summary


def build_response(data: Any, found: dict[str, list[Any]]) -> str:
    raw_message = safe_json_dumps(data)
    all_found = {key: values for key, values in found.items() if values}
    token_values = first_values(found, "token")

    header_lines = [
        "=== ДАННЫЕ ФАЙЛА MAX ===",
        "",
        format_values("FILE_ID", first_values(found, "file_id") or first_values(found, "id")),
        "",
        format_values("TOKEN", token_values),
        "",
        format_values("ATTACHMENT_ID", first_values(found, "attachment_id")),
        "",
        format_values("TYPE", first_values(found, "type")),
        "",
        format_values("FILENAME", first_values(found, "filename") or first_values(found, "name")),
        "",
        format_values("SIZE", first_values(found, "size")),
        "",
        format_values("MIME_TYPE", first_values(found, "mime_type")),
        "",
    ]

    if not token_values:
        header_lines.extend(
            [
                "TOKEN не найден в объекте сообщения. Ниже raw message - посмотри структуру.",
                "",
            ]
        )

    header_lines.extend(
        [
            "=== ВСЕ НАЙДЕННЫЕ ПОЛЯ ===",
            safe_json_dumps(all_found),
            "",
            "=== RAW MESSAGE ===",
            raw_message,
        ]
    )
    return "\n".join(header_lines)


def split_message(text: str, limit: int = MESSAGE_PART_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + limit // 2:
                end = newline + 1
        parts.append(text[start:end].strip())
        start = end
    return parts


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def send_text(bot: Any, message: Any, text: str) -> None:
    event_message = getattr(message, "message", None)
    if event_message is not None:
        for method_name in ("answer", "reply"):
            method = getattr(event_message, method_name, None)
            if callable(method):
                await maybe_await(method(text))
                return

    plain = object_to_plain_data(message)
    chat_id = None
    if isinstance(plain, dict):
        chat = plain.get("chat")
        if isinstance(chat, dict):
            chat_id = chat.get("id") or chat.get("chat_id")
        chat_id = chat_id or plain.get("chat_id") or plain.get("dialog_id") or plain.get("peer_id")

    direct_answer = getattr(message, "answer", None)
    if callable(direct_answer):
        await maybe_await(direct_answer(text))
        return

    direct_reply = getattr(message, "reply", None)
    if callable(direct_reply):
        await maybe_await(direct_reply(text))
        return

    for method_name in ("send_message", "send_text", "send"):
        method = getattr(bot, method_name, None)
        if not callable(method):
            continue
        try:
            if chat_id is not None:
                await maybe_await(method(chat_id=chat_id, text=text))
            else:
                await maybe_await(method(text=text))
            return
        except TypeError:
            try:
                if chat_id is not None:
                    await maybe_await(method(chat_id, text))
                else:
                    await maybe_await(method(text))
                return
            except TypeError:
                continue

    raise RuntimeError("Cannot find a supported method to send a MAX message")


async def send_long_text(bot: Any, message: Any, text: str) -> None:
    parts = split_message(text)
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        prefix = f"Часть {index}/{total}\n\n" if total > 1 else ""
        await send_text(bot, message, f"{prefix}{part}")


def is_admin_allowed(user_id: Any, admin_ids: set[int]) -> bool:
    if not admin_ids:
        return True
    try:
        return int(user_id) in admin_ids
    except (TypeError, ValueError):
        return False


def make_handlers(bot: Any, admin_ids: set[int]) -> tuple[Callable[..., Any], Callable[..., Any]]:
    async def handle_start(message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            event_data = object_to_plain_data(message)
            data = object_to_plain_data(extract_user_message_object(message))
            user_id = get_user_id(event_data) or get_user_id(data)
            logger.info("Received /start from user_id=%s", user_id)
            if not is_admin_allowed(user_id, admin_ids):
                logger.warning("Ignoring /start from non-admin user_id=%s", user_id)
                return
            await send_text(
                bot,
                message,
                "Привет. Отправьте мне аудио/файл/документ, и я верну file_id, token и raw-данные вложения.",
            )
        except Exception:
            logger.exception("Failed to handle /start")

    async def handle_any_message(message: Any, *args: Any, **kwargs: Any) -> None:
        try:
            event_data = object_to_plain_data(message)
            data = object_to_plain_data(extract_user_message_object(message))
            user_id = get_user_id(event_data) or get_user_id(data)
            text = get_message_text(event_data) or get_message_text(data)
            if text == "/start":
                await handle_start(message, *args, **kwargs)
                return

            if not is_admin_allowed(user_id, admin_ids):
                logger.warning("Ignoring message from non-admin user_id=%s", user_id)
                return

            has_attachments = looks_like_attachment_data(data)
            logger.info("Received message from user_id=%s; has_attachments=%s", user_id, has_attachments)
            logger.info("Raw MAX message object:\n%s", safe_json_dumps(data))

            if not has_attachments:
                await send_text(
                    bot,
                    message,
                    "Пришлите аудиофайл или документ. Я попробую достать file_id/token из вложения.",
                )
                return

            found = find_keys_recursive(data, TARGET_KEYS)
            logger.info("Found keys: %s", safe_json_dumps(summarize_found_for_log(found)))

            response = build_response(data, found)
            await send_long_text(bot, message, response)
        except Exception as exc:
            logger.exception("Failed to handle incoming MAX message")
            try:
                await send_text(bot, message, f"Ошибка обработки сообщения: {exc}")
            except Exception:
                logger.exception("Failed to send processing error to user")

    return handle_start, handle_any_message


def register_handlers(dispatcher: Any, start_handler: Callable[..., Any], any_handler: Callable[..., Any]) -> None:
    """
    Register handlers against common dispatcher APIs used by bot frameworks.
    maxapi versions differ, so this keeps the temporary bot autonomous.
    """
    message_created = getattr(dispatcher, "message_created", None)
    if callable(message_created):
        try:
            message_created(Command("start"))(start_handler)
            message_created()(any_handler)
            logger.info("Handlers registered via dispatcher.message_created")
            return
        except TypeError:
            try:
                message_created(commands=["start"])(start_handler)
                message_created()(any_handler)
                logger.info("Handlers registered via dispatcher.message_created")
                return
            except TypeError:
                pass

    message_attr = getattr(dispatcher, "message", None)
    if callable(message_attr):
        try:
            message_attr(commands=["start"])(start_handler)
            message_attr()(any_handler)
            logger.info("Handlers registered via dispatcher.message decorator")
            return
        except TypeError:
            try:
                message_attr(command="start")(start_handler)
                message_attr()(any_handler)
                logger.info("Handlers registered via dispatcher.message decorator")
                return
            except TypeError:
                pass

    for method_name in ("message_handler", "message"):
        method = getattr(dispatcher, method_name, None)
        if not callable(method):
            continue
        try:
            method(commands=["start"])(start_handler)
            method()(any_handler)
            logger.info("Handlers registered via dispatcher.%s decorator", method_name)
            return
        except TypeError:
            continue

    for method_name in ("add_handler", "register_message_handler", "register_handler"):
        method = getattr(dispatcher, method_name, None)
        if not callable(method):
            continue
        try:
            method(start_handler)
            method(any_handler)
            logger.info("Handlers registered via dispatcher.%s", method_name)
            return
        except TypeError:
            continue

    logger.warning(
        "Could not auto-register handlers. Dispatcher type=%s, maxapi=%s",
        type(dispatcher),
        getattr(maxapi, "__version__", "unknown"),
    )


async def start_polling(dispatcher: Any, bot: Any) -> None:
    start_polling_method = getattr(dispatcher, "start_polling")
    result = start_polling_method(bot)
    await maybe_await(result)


def main() -> None:
    load_dotenv()

    token = os.getenv("MAX_BOT_TOKEN")
    if not token:
        print("Ошибка: MAX_BOT_TOKEN не задан в .env", file=sys.stderr)
        sys.exit(1)

    admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS"))
    logger.info("Starting MAX file data bot")
    logger.info("ADMIN_IDS filter: %s", sorted(admin_ids) if admin_ids else "disabled")

    try:
        bot = Bot(token=token)
    except TypeError:
        bot = Bot(token)

    dispatcher = Dispatcher()
    start_handler, any_handler = make_handlers(bot, admin_ids)
    register_handlers(dispatcher, start_handler, any_handler)

    logger.info("Polling started via maxapi.Dispatcher.start_polling(bot)")
    try:
        asyncio.run(start_polling(dispatcher, bot))
    except KeyboardInterrupt:
        logger.info("Bot stopped by KeyboardInterrupt")
    except Exception:
        logger.exception("Polling stopped with error")
        raise


if __name__ == "__main__":
    main()
