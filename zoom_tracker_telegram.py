import requests
from bs4 import BeautifulSoup
import json
import time
import hashlib
from datetime import datetime
import os
import asyncio
import redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN no está configurado. Crea un archivo .env con tu token.")
STATE_FILE = os.getenv("STORAGE_PATH", "tracking_state.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

AWAITING_LINK = 1

TRACKING_INTERVAL = 1800

REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
else:
    redis_client = None

def load_state():
    if redis_client:
        try:
            data = redis_client.get("bot_state")
            if data:
                return json.loads(data)
        except Exception as e:
            print(f"Error loading from Redis: {e}")
        return {"packages": {}, "tracking_tasks": {}}
    else:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"packages": {}, "tracking_tasks": {}}

def save_state(state):
    if redis_client:
        try:
            redis_client.set("bot_state", json.dumps(state, ensure_ascii=False))
        except Exception as e:
            print(f"Error saving to Redis: {e}")
    else:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

def extract_guia_from_url(url):
    try:
        if "nro-guia=" in url:
            return url.split("nro-guia=")[1].split("&")[0]
        if "codigo=" in url:
            return url.split("codigo=")[1].split("&")[0]
        return "N/A"
    except:
        return "N/A"

def fetch_page(url):
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.text

def parse_tracking(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    data = {
        "eventos": [],
        "numero_guia": "N/A",
        "entregado": False,
        "nombre_cliente": None,
        "raw_html": html[:5000]
    }
    
    guia_elem = soup.find("input", {"name": "nro-guia"}) or soup.find("input", {"id": "nro-guia"})
    if guia_elem:
        val = guia_elem.get("value", "N/A")
        if val and val != "N/A":
            data["numero_guia"] = val
            
    if data["numero_guia"] == "N/A" or not data["numero_guia"]:
        td_titulo = soup.find("td", string=lambda text: text and "N° DE GUÍA" in text.upper())
        if not td_titulo:
            td_titulo = soup.find("td", class_="titulo", string=lambda text: text and "DE GUÍA" in text.upper())
            
        if td_titulo:
            next_td = td_titulo.find_next_sibling("td")
            if next_td:
                val = next_td.get_text(strip=True)
                if val:
                    data["numero_guia"] = val

    if (data["numero_guia"] == "N/A" or not data["numero_guia"]) and url:
        data["numero_guia"] = extract_guia_from_url(url)
    
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cols) >= 6 and cols[0].isdigit():
                data["eventos"].append({
                    "id": cols[0],
                    "fecha": cols[1],
                    "hora": cols[2],
                    "estatus": cols[3],
                    "ubicacion": cols[4],
                    "oficina": cols[5]
                })
                if "entregado" in cols[3].lower() and "cliente" in cols[3].lower():
                    data["entregado"] = True
                    if len(cols) > 6:
                        data["nombre_cliente"] = cols[6]
    
    for event in data["eventos"]:
        if "entregado" in event["estatus"].lower() and "cliente" in event["estatus"].lower():
            data["entregado"] = True
            break
    
    return data

def get_state_hash(data):
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

def load_previous_state(guia_hash):
    state = load_state()
    return state.get("packages", {}).get(guia_hash)

def save_package_state(guia_hash, data, current_hash, url):
    state = load_state()
    if "packages" not in state:
        state["packages"] = {}
    state["packages"][guia_hash] = {
        "last_update": datetime.now().isoformat(),
        "hash": current_hash,
        "data": data,
        "url": url,
        "numero_guia": data.get("numero_guia", "N/A"),
        "retirado": state["packages"].get(guia_hash, {}).get("retirado", False),
        "notified_entregado": state["packages"].get(guia_hash, {}).get("notified_entregado", False),
        "notified_entregado_previously": state["packages"].get(guia_hash, {}).get("notified_entregado_previously", False),
    }
    save_state(state)

async def send_message(update: Update, text: str, keyboard=None):
    reply_markup = keyboard if keyboard else ReplyKeyboardRemove()
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = """
✅ <b>Bot de Tracking ZOOM activo</b>

Este bot rastrea tus paquetes de Zoom y te notifica cuando haya cambios.

<b>Comandos disponibles:</b>

📦 /rastrear - Agregar un paquete para rastrear
📋 /paquetes - Ver todos los paquetes activos
❌ /detener - Dejar de rastrear un paquete
🔄 /estado - Ver estado actual de un paquete

<i>El bot verificará automáticamente cada 30 minutos.</i>
"""
    keyboard = ReplyKeyboardMarkup([
        ["📦 Rastrear nuevo paquete"],
        ["📋 Ver paquetes", "❌ Detener"],
    ], resize_keyboard=True)
    await send_message(update, welcome, keyboard)

async def rastrear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📦 Por favor, <b>envía el link de tracking</b> de Zoom.\n\n"
        "Ejemplo: https://zoom.red/tracking-de-envios-personas/?nro-guia=1653550139...",
        parse_mode="HTML"
    )
    return AWAITING_LINK

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    
    if not url.startswith("http"):
        await update.message.reply_text("❌ Por favor, envía un link válido.")
        return AWAITING_LINK
    
    chat_id = update.message.chat_id
    
    try:
        await update.message.reply_text("🔍 Verificando el enlace...")
        html = fetch_page(url)
        data = parse_tracking(html, url)
        
        guia_hash = hashlib.md5(url.encode()).hexdigest()
        current_hash = get_state_hash(data)
        
        state = load_state()
        if guia_hash in state.get("packages", {}):
            if state["packages"][guia_hash].get("retirado"):
                await update.message.reply_text(
                    "⚠️ Este paquete fue marcado como <b>retirado</b> anteriormente.\n"
                    "Usa /rastrear para agregarlo de nuevo.",
                    parse_mode="HTML"
                )
                return ConversationHandler.END
        
        save_package_state(guia_hash, data, current_hash, url)
        
        state = load_state()
        package_key = str(chat_id)
        if "tracking_tasks" not in state:
            state["tracking_tasks"] = {}
        if package_key not in state["tracking_tasks"]:
            state["tracking_tasks"][package_key] = {}
        state["tracking_tasks"][package_key][guia_hash] = True
        save_state(state)
        
        latest = data["eventos"][0] if data.get("eventos") else {}
        status = f"""
✅ <b>Paquete agregado exitosamente!</b>

📦 Guía: <code>{data.get('numero_guia', 'N/A')}</code>
📍 Estado: <b>{latest.get('estatus', 'N/A')}</b>
📅 Última actualización: {latest.get('fecha', 'N/A')} {latest.get('hora', 'N/A')}
📍 Ubicación: {latest.get('ubicacion', 'N/A')}
"""
        
        if data.get("entregado"):
            status += "\n⚠️ <b>NOTA:</b> Este paquete ya aparece como entregado."
            if data.get("nombre_cliente"):
                status += f"\n👤 Cliente: {data['nombre_cliente']}"
        
        status += "\n\n🔄 Se verificará automáticamente cada 30 minutos."
        
        await update.message.reply_text(status, parse_mode="HTML")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error al procesar el enlace: {e}")
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operación cancelada.")
    return ConversationHandler.END

async def listar_paquetes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    packages = state.get("packages", {})
    
    if not packages:
        await update.message.reply_text("📭 No hay paquetes registrados.")
        return
    
    chat_id = str(update.message.chat_id)
    state = load_state()
    active_packages = []
    
    for guia_hash, pkg in packages.items():
        if not pkg.get("retirado"):
            active_packages.append((guia_hash, pkg))
    
    if not active_packages:
        await update.message.reply_text("📭 No hay paquetes activos.")
        return
    
    message = "📋 <b>Paquetes en seguimiento:</b>\n\n"
    
    for i, (guia_hash, pkg) in enumerate(active_packages, 1):
        data = pkg.get("data", {})
        latest = data.get("eventos", [{}])[0] if data.get("eventos") else {}
        entregado = "✅" if data.get("entregado") else "⏳"
        
        message += f"{i}. {entregado} <b>Guía:</b> <code>{pkg.get('numero_guia', 'N/A')}</code>\n"
        message += f"   📍 Estado: {latest.get('estatus', 'N/A')}\n"
        message += f"   📅 {latest.get('fecha', 'N/A')} {latest.get('hora', 'N/A')}\n"
        if pkg.get("retirado"):
            message += f"   🛑 <i>Retirado</i>\n"
        message += "\n"
    
    await update.message.reply_text(message, parse_mode="HTML")

async def detener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    packages = state.get("packages", {})
    
    if not packages:
        await update.message.reply_text("📭 No hay paquetes para detener.")
        return
    
    active_packages = [(h, p) for h, p in packages.items() if not p.get("retirado")]
    
    if not active_packages:
        await update.message.reply_text("📭 No hay paquetes activos para detener.")
        return
    
    keyboard = []
    for guia_hash, pkg in active_packages:
        btn_text = f"❌ {pkg.get('numero_guia', 'N/A')[:20]}..."
        keyboard.append([btn_text])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        "❌ Selecciona el paquete que deseas <b>marcar como retirado</b>:\n\n"
        "(Una vez marcado, el bot dejará de rastrear este paquete)",
        parse_mode="HTML",
        reply_markup=reply_markup
    )
    
    context.user_data["awaiting_retirar"] = True
    context.user_data["active_packages"] = [(h, p) for h, p in active_packages]

async def handle_detener_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_retirar"):
        return
    
    selected_text = update.message.text
    active_packages = context.user_data.get("active_packages", [])
    
    for guia_hash, pkg in active_packages:
        btn_text = f"❌ {pkg.get('numero_guia', 'N/A')[:20]}..."
        if selected_text == btn_text or selected_text == f"❌ {pkg.get('numero_guia', 'N/A')}":
            state = load_state()
            if guia_hash in state["packages"]:
                state["packages"][guia_hash]["retirado"] = True
                save_state(state)
                
                await update.message.reply_text(
                    f"✅ Paquete <b>{pkg.get('numero_guia', 'N/A')}</b> marcado como <b>retirado</b>.\n\n"
                    "🛑 El bot dejará de rastrear este paquete.\n"
                    "Usa /rastrear para agregarlo de nuevo cuando lo necesites.",
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardRemove()
                )
                context.user_data["awaiting_retirar"] = False
                return
    
    context.user_data["awaiting_retirar"] = False

async def estado_paquete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    packages = state.get("packages", {})
    
    if not packages:
        await update.message.reply_text("📭 No hay paquetes registrados.")
        return
    
    active_packages = [(h, p) for h, p in packages.items() if not p.get("retirado")]
    
    if not active_packages:
        await update.message.reply_text("📭 No hay paquetes activos.")
        return
    
    message = "📦 <b>Estado actual de paquetes:</b>\n\n"
    
    for guia_hash, pkg in active_packages:
        data = pkg.get("data", {})
        latest = data.get("eventos", [{}])[0] if data.get("eventos") else {}
        
        message += f"📦 Guía: <code>{pkg.get('numero_guia', 'N/A')}</code>\n"
        message += f"   📍 Estado: <b>{latest.get('estatus', 'N/A')}</b>\n"
        message += f"   📅 {latest.get('fecha', 'N/A')} {latest.get('hora', 'N/A')}\n"
        message += f"   📍 {latest.get('ubicacion', 'N/A')} - {latest.get('oficina', 'N/A')}\n"
        
        if data.get("entregado"):
            message += f"   ✅ <b>ENTREGADO</b>"
            if data.get("nombre_cliente"):
                message += f" a {data['nombre_cliente']}"
            message += "\n"
        
        message += "\n"
    
    await update.message.reply_text(message, parse_mode="HTML")

async def track_single_package(bot, chat_id, guia_hash, pkg_info):
    url = pkg_info.get("url")
    previous_data = pkg_info.get("data", {})
    previous_hash = pkg_info.get("hash")
    
    try:
        html = fetch_page(url)
        data = parse_tracking(html, url)
        current_hash = get_state_hash(data)
        
        save_package_state(guia_hash, data, current_hash, url)
        
        if previous_hash and current_hash != previous_hash:
            old_ids = {e["id"] for e in previous_data.get("eventos", [])}
            new_ids = {e["id"] for e in data.get("eventos", [])}
            new_event_ids = new_ids - old_ids
            new_events_map = {e["id"]: e for e in data.get("eventos", [])}
            
            if new_event_ids:
                lines = []
                lines.append("🔔 <b>CAMBIO DETECTADO!</b>")
                lines.append(f"📦 Guía: <code>{data.get('numero_guia', 'N/A')}</code>")
                lines.append("")
                
                for eid in sorted(new_event_ids):
                    event = new_events_map[eid]
                    lines.append(f"📍 <b>#{event['id']} {event['estatus']}</b>")
                    lines.append(f"   📅 {event['fecha']} {event['hora']}")
                    lines.append(f"   📍 {event['ubicacion']} - {event['oficina']}")
                    lines.append("")
                
                lines.append(f"✅ Último: <b>{data['eventos'][0].get('estatus', 'N/A')}</b>")
                
                await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
        
        state = load_state()
        if guia_hash in state.get("packages", {}):
            was_notified_before = state["packages"][guia_hash].get("notified_entregado_previously", False)
            
            if data.get("entregado") and not was_notified_before:
                lines = []
                lines.append("🎉 <b>¡PAQUETE ENTREGADO!</b>")
                lines.append(f"📦 Guía: <code>{data.get('numero_guia', 'N/A')}</code>")
                
                if data.get("nombre_cliente"):
                    lines.append(f"👤 Recibido por: <b>{data['nombre_cliente']}</b>")
                
                latest = data["eventos"][0] if data.get("eventos") else {}
                lines.append(f"📍 Última ubicación: {latest.get('ubicacion', 'N/A')}")
                lines.append(f"📅 {latest.get('fecha', 'N/A')} {latest.get('hora', 'N/A')}")
                
                state["packages"][guia_hash]["notified_entregado"] = True
                state["packages"][guia_hash]["notified_entregado_previously"] = True
                save_state(state)
                
                await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
        
    except Exception as e:
        print(f"Error rastreando {guia_hash}: {e}")

async def tracking_loop(application):
    while True:
        try:
            state = load_state()
            packages = state.get("packages", {})
            
            for chat_id_key, tasks in state.get("tracking_tasks", {}).items():
                try:
                    chat_id = int(chat_id_key)
                except:
                    continue
                
                for guia_hash in tasks.keys():
                    if guia_hash in packages:
                        pkg = packages[guia_hash]
                        if not pkg.get("retirado"):
                            await track_single_package(
                                application.bot,
                                chat_id,
                                guia_hash,
                                pkg
                            )
            
        except Exception as e:
            print(f"Error en tracking loop: {e}")
        
        await asyncio.sleep(TRACKING_INTERVAL)

async def post_init(application):
    asyncio.create_task(tracking_loop(application))

def main():
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("rastrear", rastrear),
            MessageHandler(filters.Regex("^📦 Rastrear nuevo paquete$"), rastrear),
        ],
        states={
            AWAITING_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("paquetes", listar_paquetes))
    application.add_handler(MessageHandler(filters.Regex("^📋 Ver paquetes$"), listar_paquetes))
    application.add_handler(CommandHandler("detener", detener))
    application.add_handler(MessageHandler(filters.Regex("^❌ Detener$"), detener))
    application.add_handler(CommandHandler("estado", estado_paquete))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_detener_response))
    
    print("=" * 50)
    print("  ZOOM Tracking Bot - Telegram Commands")
    print("  Ejecutando... Presiona Ctrl+C para detener")
    print("=" * 50)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
