import os 
import io
import re
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, ChatMemberUpdatedFilter, ADMINISTRATOR, JOIN_TRANSITION
from aiogram.types import Message, ChatMemberUpdated, BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CopyMessage, DeleteMessage
import asyncio
from datetime import datetime, timedelta

from GetImage import get_image
from SendPhoto import send_photos  
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from Callbacks import handle_photos_callback
from aiogram.types import CallbackQuery

import PyPDF2

from UserRequests import UserRequests
from AsyncDbHandler import AsyncDbHandler

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ALLOWED_CHATS = set()
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "10"))
user_requests = UserRequests(max_requests=MAX_REQUESTS_PER_DAY)
bot = Bot(token=TOKEN)
dp = Dispatcher()
VIN_PATTERN = re.compile(r'(?:VIN\s*)?(ZA[RS][A-HJ-NPR-Z0-9]{14})', re.IGNORECASE)

@dp.callback_query(lambda callback_query: callback_query.data.startswith("photos:"))
async def callback_router(callback_query: CallbackQuery):
    await handle_photos_callback(callback_query, bot=bot, get_image=get_image)

@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_added_to_group(event: ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        ALLOWED_CHATS.add(event.chat.id)
        await bot.send_message(
            event.chat.id, 
            f"Бот активирован в этой группе\nЛимит запросов на пользователя: <b>{MAX_REQUESTS_PER_DAY}</b> в сутки",
            parse_mode="HTML"
        )


@dp.message()
async def handle_message(message: Message):
    if message.from_user.is_bot:
        return
    if message.content_type not in ['text', 'photo', 'document']:
        return
    # if message.chat.type in ['group', 'supergroup'] and message.chat.id in ALLOWED_CHATS:
    if message.chat.type in ['group', 'supergroup']:
        message_text = message.text or message.caption or ''
        if message_text:
            match = VIN_PATTERN.search(message_text)
            if match:
                vin = match.group(1)
                user_id = message.from_user.id

                remaining = user_requests.get_remaining_requests(user_id)
                if remaining <= 0:
                    next_reset = datetime.now() + timedelta(days=1)
                    try:
                        keyboard = InlineKeyboardMarkup()
                        keyboard.add(
                            InlineKeyboardButton(
                                text="Получить фотографии",
                                callback_data=f"photos:{vin}"
                            )
                        )                        
                        await message.reply(
                            f"Достигнут дневной лимит запросов (<b>{MAX_REQUESTS_PER_DAY}</b>). "
                            f"Следующий запрос будет доступен через <b>{next_reset.strftime('%H:%M:%S')}</b>",
                            parse_mode="HTML"
                        )
                    except TelegramBadRequest as e:
                        if "message to be replied not found" in str(e):
                            await message.chat.send_message(
                                f"Достигнут дневной лимит запросов (<b>{MAX_REQUESTS_PER_DAY}</b>). "
                                f"Следующий запрос будет доступен через <b>{next_reset.strftime('%H:%M:%S')}</b>",
                                parse_mode="HTML"
                            )
                    return
                db = AsyncDbHandler()
                msg_id_from_db = await db.GetMessageIdByVin(vin)
                bot_info = await bot.get_me()
                bot_id = bot_info.id
                if msg_id_from_db:
                     #check if message exists
                    try:
                        tmp_msg = await bot(CopyMessage(chat_id=message.chat.id, from_chat_id=message.chat.id, from_user_id=bot_id, message_id=msg_id_from_db))
                        await bot(DeleteMessage(from_user_id=bot_id, message_id=tmp_msg.message_id, chat_id=message.chat.id))
                    except TelegramBadRequest:
                    # delete it from db if it's inaccessible for the bot    
                        await db.DeleteVin(vin)
                        msg_id_from_db = None
                if not msg_id_from_db:
                    if not user_requests.add_request(user_id):
                        await message.reply("Ошибка при обработке запроса. Попробуйте позже.")
                        return
                    
                    url = f"https://www.alfaromeousa.com/hostd/windowsticker/getWindowStickerPdf.do?vin={vin}"
                    try:
                        async with httpx.AsyncClient() as client:
                            response = await client.get(url)
                            if response.status_code == 200:
                                pdf_buffer = io.BytesIO(response.content)
                                pdf_reader = PyPDF2.PdfReader(pdf_buffer)
                                text = pdf_reader.pages[0].extract_text()

                                if "Sorry, a Window Sticker is unavailable for this VIN" in text:
                                    user_requests.requests[user_id].pop()
                                    try:
                                        sent_msg = await message.reply("Window sticker недоступен для данного VIN")
                                    except TelegramBadRequest as e:
                                        if "message to be replied not found" in str(e):
                                           sent_msg = await  message.chat.send_message("Window sticker недоступен для данного VIN")
                                        else:
                                            raise
                                    await db.AddVIN(vin, sent_msg.message_id)
                                else:
                                    pdf_file = BufferedInputFile(
                                                        response.content,
                                                        filename=f"{vin}.pdf"
                                                    )
                                    try:
                                        keyboard = InlineKeyboardMarkup(
                                            inline_keyboard=[ 
                                                [
                                                    InlineKeyboardButton(
                                                        text="Получить фотографии битка", 
                                                        callback_data=f"photos:{vin}"  
                                                    )
                                                ]
                                            ]
                                        )                                      
                                        sent_msg = await message.reply_document(
                                            document=pdf_file,
                                            caption=f"Window sticker for VIN: <b>{vin}</b>\nОсталось запросов сегодня: <b>{remaining-1}</b>",
                                            parse_mode="HTML",
                                            reply_markup=keyboard
                                        )                                        
                                    except TelegramBadRequest as e:
                                        if "message to be replied not found" in str(e):
                                            sent_msg = await message.chat.send_document(
                                                document=pdf_file,
                                                caption=f"Window sticker for VIN: <b>{vin}</b>\nОсталось запросов сегодня: <b>{remaining-1}</b>",
                                                parse_mode="HTML",
                                                reply_markup=keyboard
                                            )
                                        else:
                                            raise
                                    await db.AddVIN(vin, sent_msg.message_id)
                            else:
                                user_requests.requests[user_id].pop()
                                try:
                                    await message.reply("Ошибка загрузки файла")
                                except TelegramBadRequest as e:
                                    if "message to be replied not found" in str(e):
                                        await message.chat.send_message("Ошибка загрузки файла")
                                    else:
                                        raise 
                    except Exception as e:
                        user_requests.requests[user_id].pop()
                        await message.reply(f"Произошла ошибка при отправке pdf: {str(e)}")
                else:
                    try:
                        await message.reply(f"Ссылка на сообщение с pdf:\nhttps://t.me/{message.chat.username}/{msg_id_from_db}")
                    except TelegramBadRequest as e:
                        if "message to be replied not found" in str(e):
                            await message.chat.send_message(f"Ссылка на сообщение с pdf:\nhttps://t.me/{message.chat.username}/{msg_id_from_db}")
                        else:
                            raise 

                # try:
                #     eper_client = FiatPartsClient(headers=headers, cookies=cookies)
                #     pdf_generator = FiatPartsPDFGenerator()
                #     result = await eper_client.get_full_vin_info(vin, session_id)

                #     pdf_report = BufferedInputFile(
                #                     pdf_generator.create_pdf(result),
                #                     filename=f"{vin}.pdf"
                #                 )
                #     await message.reply_document(
                #                         document=pdf_report,
                #                         caption=f"Комплектация по VIN: <b>{vin}</b>\nОсталось запросов сегодня: <b>{remaining-1}</b>",
                #                         parse_mode="HTML"
                #                     )
                    
                # except Exception as e:
                #     user_requests.requests[user_id].pop()

async def main():
    print(f"Бот запущен с лимитом {MAX_REQUESTS_PER_DAY} запросов в сутки на пользователя")
    db = AsyncDbHandler()
    await db.init_async()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())