import warnings
# Убиваме спама за пакета още преди да сме импортирали каквото и да било
warnings.filterwarnings("ignore", category=FutureWarning)

import re
import discord
import google.generativeai as genai
import aiohttp
import json
import os
from discord.ext import tasks
from dotenv import load_dotenv

# Библиотеки за Google Drive
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

# --- КОНФИГУРАЦИЯ ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MY_DISCORD_ID = int(os.getenv("MY_DISCORD_ID"))
MEMORY_FILE = "genadi_memory.json"
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

# ID-то на твоя файл в Google Drive
DRIVE_FILE_ID = os.getenv("DRIVE_FILE_ID")

genai.configure(api_key=GEMINI_API_KEY)

# --- ИНСТРУМЕНТИ ЗА GOOGLE DRIVE (САМО UPDATE / GET ЛОГИКА) ---

def get_drive_service():
    if not GOOGLE_CREDS_JSON:
        return None
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def download_from_drive():
    """Тегли последната памет от Drive при стартиране на контейнера."""
    service = get_drive_service()
    if not service or not DRIVE_FILE_ID:
        return
    try:
        content = service.files().get_media(fileId=DRIVE_FILE_ID, supportsAllDrives=True).execute()
        with open(MEMORY_FILE, "wb") as f:
            f.write(content)
        print("✅ [Drive] Старата памет е изтеглена успешно от облака!")
    except Exception as e:
        print(f"⚠️ [Drive Info]: Файлът е празен или има грешка при първоначално теглене: {e}")

def upload_to_drive():
    """Само обновява съществуващия файл чрез update, заобикаляйки 0GB квотата."""
    service = get_drive_service()
    if not service or not DRIVE_FILE_ID:
        print("⚠️ Липсва DRIVE_FILE_ID или Google ключове!")
        return
    try:
        media = MediaFileUpload(MEMORY_FILE, mimetype='application/json')
        service.files().update(
            fileId=DRIVE_FILE_ID, 
            media_body=media,
            supportsAllDrives=True
        ).execute()
        print("✅ [Drive] Паметта е синхронизирана успешно в облака!")
    except Exception as e:
        print(f"❌ [Drive Error]: {e}")

# --- ИНСТРУМЕНТИ ЗА DISCORD ---

def list_channels():
    return [{"name": c.name, "id": str(c.id)} for guild in client.guilds for c in guild.text_channels]

def list_members():
    return [{"name": m.name, "id": str(m.id)} for guild in client.guilds for m in guild.members if not m.bot]

model = genai.GenerativeModel(
    model_name='gemini-3.1-pro-preview',
    tools=[list_channels, list_members],
    system_instruction=(
        "Ти си Genadi - личният асистент на Илия. Помагай му с всичко (университет, БМВ-та, проекти). "
        "Discord ID-тата преписвай точно като текст (String)."
    )
)

client = discord.Client(intents=discord.Intents.all())
chat_sessions = {} 

def save_memory_to_local():
    serializable_memory = {}
    for ch_id, session in chat_sessions.items():
        history_data = []
        for content in session.history:
            history_data.append({
                "role": content.role,
                "parts": [part.text for part in content.parts if hasattr(part, 'text')]
            })
        serializable_memory[str(ch_id)] = history_data
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable_memory, f, ensure_ascii=False, indent=4)
    return MEMORY_FILE

def load_memory_from_local():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for ch_id, history in data.items():
                    chat_sessions[int(ch_id)] = model.start_chat(history=history, enable_automatic_function_calling=True)
            print("✅ Локалната памет е заредена в RAM.")
        except: pass

@tasks.loop(hours=1)
async def hourly_backup():
    save_memory_to_local()
    upload_to_drive()

async def process_attachments(attachments):
    processed_files = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            async with session.get(att.url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    processed_files.append({"mime_type": att.content_type, "data": data})
    return processed_files

@client.event
async def on_ready():
    # 1. Теглим последния бекъп от Drive
    download_from_drive()
    # 2. Зареждаме го
    load_memory_from_local()
    # 3. Пускаме таймера
    if not hourly_backup.is_running():
        hourly_backup.start()
    print(f'Генади е онлайн и напълно брониран!')

@client.event
async def on_message(message):
    if message.author.id != MY_DISCORD_ID:
        return

    if message.content == "!sync":
        async with message.channel.typing():
            save_memory_to_local()
            upload_to_drive()
            await message.channel.send("📁 Синхронизирано директно върху твоя файл в Google Drive!")
        return

    if client.user.mentioned_in(message):
        channel_id = message.channel.id
        async with message.channel.typing():
            if channel_id not in chat_sessions:
                chat_sessions[channel_id] = model.start_chat(history=[], enable_automatic_function_calling=True)
            
            user_input = message.content.replace(f'<@!{client.user.id}>', '').replace(f'<@{client.user.id}>', '').strip()
            prompt_parts = [user_input]
            if message.attachments:
                files = await process_attachments(message.attachments)
                for f in files: prompt_parts.append(f)

            try:
                response = chat_sessions[channel_id].send_message(prompt_parts)
                res_text = response.text
                save_memory_to_local()
                
                limit = 1900
                for i in range(0, len(res_text), limit):
                    await message.channel.send(res_text[i:i+limit])
            except Exception as e:
                await message.channel.send(f"❌ Грешка: {e}")

client.run(DISCORD_TOKEN)