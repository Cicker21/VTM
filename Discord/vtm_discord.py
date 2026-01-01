# -*- coding: utf-8 -*-
"""
vtm_discord.py — Bot de música para Discord con comandos de voz y texto.
Decodificado de vtm_core.py.
"""

import discord
from discord.ext import commands
import os
import json
import logging
import asyncio
import io
import time
import traceback
import speech_recognition as sr
import random
import discord.voice_client
import discord.gateway
from discord.ext import voice_recv, tasks
import array
import threading
import shutil
import uuid
import re
from yt_dlp import YoutubeDL
import tempfile
import pyttsx3

# --- Logger de yt-dlp ---
class YtdlLogger:
    def debug(self, msg): pass
    def warning(self, msg):
        ignore_keywords = ["JavaScript runtime", "web_safari", "PO Token", "web client", "ios client"]
        if not any(k in msg for k in ignore_keywords):
            logging.warning(f"[yt-dlp] {msg}")
    def error(self, msg):
        logging.error(f"[yt-dlp ERROR] {msg}")

# --- Filtrado y Búsqueda ---
def is_content_allowed(info, config):
    if not config.get("filters_enabled", True):
        return True
    
    title = info.get("title", "").lower()
    duration = info.get("duration", 0)
    forced = config.get("forced_keyword")

    if forced and forced.lower() not in title:
        logging.info(f"🚫 Saltado (No contiene forzada '{forced}'): {title}")
        return False

    blacklist = config.get("blacklisted_keywords", [])
    for word in blacklist:
        if word.lower() in title:
            logging.info(f"🚫 Saltado (Blacklist '{word}'): {title}")
            return False

    max_dur = config.get("max_duration_seconds", 600)
    if duration > 0 and duration > max_dur:
        logging.info(f"🚫 Saltado (Largo {duration}s > {max_dur}s): {title}")
        return False

    return True

def normalize_title(text):
    """Normaliza el título para evitar repeticiones por variaciones pequeñas."""
    if not text: return ""
    # Quitar sufijos comunes de YouTube
    text = re.sub(r'\(?official (video|audio|music|lyrics)?\)?', '', text, flags=re.I)
    text = re.sub(r'\[?official (video|audio|music|lyrics)?\]?', '', text, flags=re.I)
    text = re.sub(r'\(?video oficial\)?', '', text, flags=re.I)
    # Quitar puntuación y espacios extra
    text = re.sub(r'[^\w\s]', '', text)
    return " ".join(text.lower().split())

def get_search_info(query: str, index: int = 0, is_playlist: bool = False):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'logger': YtdlLogger(),
        'extract_flat': True,
        'source_address': '0.0.0.0',
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    }
    is_url = query.startswith("http")
    search_query = query if is_url else f"ytsearch10:{query}"
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(search_query, download=False)
            if not res: return None
            
            # Ajuste crítico para URLs que son video + lista (watch?v=...&list=...)
            # Si el usuario quiere playlist o vemos un list=, intentamos forzar tipo playlist
            actual_type = res.get('_type')
            if (is_playlist or 'list=' in query) and actual_type != 'playlist':
                # A veces yt-dlp devuelve el video pero tiene la lista de entradas si no usamos extract_flat=True
                # Pero como usamos extract_flat=True, si no devolvió playlist, puede que debamos re-intentar sin el video id
                if 'list=' in query and actual_type != 'playlist':
                    playlist_id = re.search(r"list=([a-zA-Z0-9_-]+)", query)
                    if playlist_id:
                        pl_url = f"https://www.youtube.com/playlist?list={playlist_id.group(1)}"
                        res = ydl.extract_info(pl_url, download=False)
            
            if res.get('_type') == 'playlist':
                # Solo devolver la playlist completa si es una URL con list= o se pidió explícitamente.
                # Ignoramos si es un resultado de búsqueda (youtube:search) a menos que se fuerce is_playlist.
                is_search = res.get('extractor', '') == 'youtube:search'
                # REPARACIÓN: Si is_playlist es False (forzado), NO devolvemos la playlist completa.
                if is_playlist is True or (is_playlist is None and 'list=' in query and not is_search):
                    return res
            
            entries = res.get("entries", [])
            if not entries and "title" in res:
                 return res
            
            if entries and index < len(entries):
                entry = entries[index]
                # Si es un resultado de búsqueda plano, a veces le faltan datos
                if not entry.get("duration") and entry.get("url"):
                    with YoutubeDL(ydl_opts) as ydl2:
                        return ydl2.extract_info(entry['url'], download=False)
                return entry
    except Exception as e:
        logging.error(f"Error búsqueda: {e}")
    return None

def download_media(url, prefix="vtm_discord_"):
    if not url: return None
    _LOC_DIR = os.path.dirname(os.path.abspath(__file__))
    filename = f"{prefix}{uuid.uuid4()}.m4a"
    filepath = os.path.join(_LOC_DIR, filename)

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': filepath,
        'quiet': True,
        'no_warnings': True,
        'logger': YtdlLogger(),
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            if ydl.download([url]) == 0 and os.path.exists(filepath):
                return filepath
    except Exception as e:
        logging.error(f"Excepción en download_media: {e}")
    return None

def get_recommendations(video_id: str, title: str = None):
    # Intentar obtener recomendaciones directas (el panel lateral de YT)
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist'}
    try:
        with YoutubeDL(ydl_opts) as ydl:
             info = ydl.extract_info(url, download=False)
             entries = info.get("entries", [])
             if entries:
                 return [{"id": e["id"], "title": e["title"], "duration": e.get("duration")} for e in entries if e]
    except: pass

    # Fallback: Si no hay recomendaciones directas, buscar por el título
    if title:
        logging.info(f"📻 [Radio] Sin recomendaciones directas. Buscando temas relacionados con: {title}")
        search_query = f"related to {title} music"
        res = get_search_info(search_query, 0)
        # Devolvemos una lista con los resultados de la búsqueda como "recomendaciones"
        if res:
             # get_search_info ya devuelve un objeto info o entry, lo envolvemos en lista
             return [res]
    return []

def cleanup_temp_files(prefix="vtm_discord_"):
    _LOC_DIR = os.path.dirname(os.path.abspath(__file__))
    for f in os.listdir(_LOC_DIR):
        if f.startswith(prefix) and (f.endswith(".mp4") or f.endswith(".m4a")):
            try: os.remove(os.path.join(_LOC_DIR, f))
            except: pass

# --- Parser de Comandos ---
class CommandParser:
    RE_PRODUCIR = re.compile(r"(reproduce|reproducir|pon|poner|play)(me|te)?\s+(m[uú]sica\s+de\s+|la\s+canci[oó]n\s+)?(?P<q>.+)$", re.I)
    RE_PAUSA = re.compile(r"(pausa|pausar)", re.I)
    RE_CONTINUAR = re.compile(r"(continuar|reanudar|play|reproduce|reproducir|pon|continua|sigue)", re.I)
    RE_DETENER = re.compile(r"(detener|parar|stop)", re.I)
    RE_SALIR = re.compile(r"(salir|terminar)", re.I)
    RE_SIGUIENTE = re.compile(r"(siguiente|pasa)", re.I)
    RE_VOL = re.compile(r"(poner\s+|fijar\s+)?volumen\s*(al\s+|a\s+|en\s+)?(?P<n>\d{1,3})\s*%?$", re.I)
    RE_VOL_REL = re.compile(r"(?P<op>sube|subir|baja|bajar|m[áa]s|menos)\s+(el\s+|la\s+)?(volumen|m[uú]sica|audio|alto|bajo)(?P<amount>\s+un\s+poco)?", re.I)
    RE_MUTE = re.compile(r"(silencio|calla(te)?|cállate|mutea(te)?)", re.I)
    RE_UNMUTE = re.compile(r"(habla|desmut[eé]a(te)?|devuelve (el )?sonido|sonido|audio on)", re.I)
    RE_REPLAY = re.compile(r"(repite|repetir|otra vez|ponla de nuevo|reinicia(r)?|bise|reus)", re.I)
    RE_MODO = re.compile(r"modo\s+(?P<m>(navegador|directo))", re.I)
    RE_RADIO = re.compile(r"(encender|activar|apagar|desactivar|radio|auto-?dj|modo radio)\s*(radio|auto-?dj|modo radio)?\s*(?P<op>on|off)?", re.I)
    RE_FILTROS = re.compile(r"(activar|desactivar|quitar|poner|sin|con|apaga(r?)|enciende|encender)?\s*(los\s+)?filtros?\s*(?P<op>on|off)?", re.I)
    RE_INFO = re.compile(r"(info|informaci[oó]n|qu[eé]\s+suena|estado|c[oó]mo\s+est[aá]|qu[eé]\s+tal)", re.I)
    RE_VOZ = re.compile(r"(activar|desactivar|callar|hablar|voz|silencio)\s*voz?\s*(?P<op>on|off)?", re.I)
    RE_TTS_VOL = re.compile(r"voz\s+(al\s+)?(?P<n>\d{1,3})\s*%?", re.I)
    RE_FORCE = re.compile(r"forzar\s+(?P<f>on|off|.+)", re.I)
    RE_AYUDA = re.compile(r"(ayuda|help|opciones|comandos)", re.I)
    RE_QUEUE = re.compile(r"(ver\s+)?(la\s+)?(cola|lista|playlist|reproducci[oó]n)", re.I)
    RE_REMOVE = re.compile(r"(quita|quita\s+la|borra|elimina)(\s+la)?(\s+canci[oó]n)?(\s+n[uú]mero)?\s+(?P<n>\d+)", re.I)
    RE_CLEAR = re.compile(r"(limpia|vac[ií]a|borra)(\s+toda)?\s+(la\s+)?(cola|lista|playlist)", re.I)
    RE_MICRO = re.compile(r"micro(fono)?\s+(?P<n>\d+)", re.I)
    RE_LISTAR_MICROS = re.compile(r"(listar\s+)?(micros|microfonos|micrófonos|devices|dispositivos)", re.I)
    RE_TOPLIST = re.compile(r"(toplist|globaltop)(\s+(global|<@!?(?P<user>\d+)>))?", re.I)
    RE_TOPREMOVE = re.compile(r"(topremove|quita\s+del\s+top)\s+(?P<n>\d+)", re.I)

    def parse(self, text: str):
        t = text.strip().lower()

        # PRIORIDAD: Comandos de reproducción directa (pon/reproduce/forzar)
        m = self.RE_FORCE.search(t)
        if m: return ("force", {"f": m.group("f")})
        
        m = self.RE_PRODUCIR.search(t)
        if m: return ("play", {"query": m.group("q")})

        if self.RE_QUEUE.search(t): 
            if not (self.RE_REMOVE.search(t) or self.RE_CLEAR.search(t)):
                return ("queue", {})
        m = self.RE_REMOVE.search(t)
        if m: return ("remove", {"n": int(m.group("n"))})
        if self.RE_CLEAR.search(t): return ("clear", {})

        if self.RE_AYUDA.search(t): return ("help", {})
        if self.RE_LISTAR_MICROS.search(t): return ("list_mics", {})
        if self.RE_PAUSA.search(t): return ("pause", {})
        if self.RE_CONTINUAR.search(t): return ("resume", {})
        if self.RE_DETENER.search(t): return ("stop", {})
        if self.RE_SALIR.search(t): return ("exit", {})
        if self.RE_SIGUIENTE.search(t): return ("next", {})
        if self.RE_INFO.search(t): return ("info", {})
        if self.RE_MUTE.search(t): return ("mute", {})
        if self.RE_UNMUTE.search(t): return ("unmute", {})
        if self.RE_REPLAY.search(t): return ("replay", {})
        
        m = self.RE_VOL.search(t)
        if m: return ("volume", {"n": int(m.group("n"))})
        
        m = self.RE_VOL_REL.search(t)
        if m:
            op = m.group("op")
            direction = "down" if any(x in op for x in ["baja", "menos"]) else "up"
            return ("volume_rel", {"direction": direction})

        m = self.RE_MODO.search(t)
        if m: return ("mode", {"m": m.group("m")})
        
        m = self.RE_RADIO.search(t)
        if m:
            op = m.group("op")
            enabled = True
            if op == "off": enabled = False
            elif any(x in t for x in ["off", "apagar", "desactivar"]) and not (op == "on"): enabled = False
            return ("radio", {"enabled": enabled})

        m = self.RE_FILTROS.search(t)
        if m:
            op = m.group("op")
            enabled = True
            if op == "off": enabled = False
            elif any(x in t for x in ["desactivar", "quitar", "sin", "off", "apaga"]) and not (op == "on"): enabled = False
            return ("filtros", {"enabled": enabled})

        m = self.RE_VOZ.search(t)
        if m:
            enabled = not any(x in t for x in ["desactivar", "callar", "silencio", "off"])
            return ("voice", {"enabled": enabled})

        m = self.RE_TTS_VOL.search(t)
        if m: return ("tts_volume", {"n": int(m.group("n"))})

        m = self.RE_FORCE.search(t)
        if m: return ("force", {"f": m.group("f").strip()})

        m = self.RE_MICRO.search(t)
        if m: return ("set_mic", {"n": int(m.group("n"))})
        
        m = self.RE_TOPLIST.search(t)
        if m:
            user_id = m.group("user") if m.group("user") else None
            is_global = "global" in t or "globaltop" in t
            return ("toplist", {"user_id": user_id, "is_global": is_global})
        
        m = self.RE_TOPREMOVE.search(t)
        if m: return ("topremove", {"n": int(m.group("n"))})

        return (None, {})


# --- Single Instance Lock ---
_LOC_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(_LOC_DIR, "vtm_discord.lock")

def check_single_instance():
    import psutil
    pid = os.getpid()
    
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            
            if psutil.pid_exists(old_pid):
                logging.warning(f"⚠️ YA EXISTE OTRA INSTANCIA EJECUTANDOSE (PID {old_pid}).")
                logging.warning("⚠️ CERRANDO ESTA INSTANCIA PARA EVITAR CONFLICTOS.")
                print(f"ERROR: Ya corre otra instancia (PID {old_pid}). Cierra la otra antes de abrir esta.")
                # Intentar matar la antigua si el usuario quiere "fuerza bruta", pero mejor salir.
                # Para este caso, vamos a ser agresivos y matar la vieja para que 'esta' funcione (usuario desesperado)
                try:
                    p = psutil.Process(old_pid)
                    p.terminate()
                    logging.info(f"💀 Instancia anterior (PID {old_pid}) eliminada.")
                    time.sleep(1)
                except:
                    logging.error("❌ No pude cerrar la instancia anterior. Saliendo.")
                    os._exit(1)
        except Exception as e:
            logging.error(f"Error checking lock file: {e}")
            pass

    with open(LOCK_FILE, 'w') as f:
        f.write(str(pid))
    
    # Registrar limpieza a la salida
    import atexit
    def remove_lock():
        if os.path.exists(LOCK_FILE):
            try: os.remove(LOCK_FILE)
            except: pass
    atexit.register(remove_lock)

# --- Configuración de Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (Discord) %(message)s",
    force=True
)
# Silenciar spam de paquetes RTCP y CryptoErrors (común en voice-recv)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.CRITICAL)  # Silenciar CryptoErrors completamente
logging.getLogger("discord.ext.voice_recv").setLevel(logging.ERROR)

DISCORD_PREFIX = "vtm_discord_"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DISCORD_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "config_discord.json")
FFMPEG_PATH = r"C:\Users\todom\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
TOP_FILE = os.path.join(_SCRIPT_DIR, "top.json")

def load_discord_config():
    defaults = {
        "discord_token": "TU_TOKEN_AQUI",
        "volume": 0.05,
        "forced_keyword": None,
        "filters_enabled": True,
        "listen_enabled": False,
        "hotwords": ["rafa"],
        "hotword": "rafa" # Legacy
    }
    if not os.path.exists(DISCORD_CONFIG_FILE): return defaults
    try:
        with open(DISCORD_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Merge defaults
            for k, v in defaults.items():
                if k not in data: data[k] = v
            # User request: Do not persist forced_keyword on startup
            # Normalize hotwords
            hw_list = data.get("hotwords", [])
            hw_single = data.get("hotword")
            if isinstance(hw_list, list) and hw_list:
                data["hotwords"] = [h.lower() for h in hw_list if isinstance(h, str)]
            elif isinstance(hw_single, str):
                data["hotwords"] = [hw_single.lower()]
            else:
                data["hotwords"] = [defaults["hotword"]]

            # User request: Do not persist forced_keyword on startup
            data["forced_keyword"] = None
            return data
    except: return defaults

def save_discord_config(config):
    try:
        with open(DISCORD_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        logging.info("💾 Configuración de Discord guardada.")
    except Exception as e:
        logging.error(f"Error guardando config: {e}")

def load_top_data():
    """Load top.json data"""
    defaults = {"user_tops": {}, "global_top": []}
    if not os.path.exists(TOP_FILE):
        save_top_data(defaults)
        return defaults
    try:
        with open(TOP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return defaults

def save_top_data(data):
    """Save top.json data"""
    try:
        with open(TOP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error guardando top.json: {e}")

def add_to_user_top(user_id, song_info):
    """Add song to user's top (max 5, FIFO)"""
    data = load_top_data()
    user_id_str = str(user_id)
    
    if user_id_str not in data["user_tops"]:
        data["user_tops"][user_id_str] = []
    
    # Create song entry
    song_entry = {
        "title": song_info.get("title", "Unknown"),
        "id": song_info.get("id", ""),
        "added_by": user_id_str
    }
    
    # Remove if already exists (avoid duplicates)
    data["user_tops"][user_id_str] = [s for s in data["user_tops"][user_id_str] if s["id"] != song_entry["id"]]
    
    # Add to end
    data["user_tops"][user_id_str].append(song_entry)
    
    # Keep only last 5
    if len(data["user_tops"][user_id_str]) > 5:
        data["user_tops"][user_id_str] = data["user_tops"][user_id_str][-5:]
    
    save_top_data(data)

def add_to_global_top(song_info, user_id):
    """Add song to global top (max 20, FIFO)"""
    data = load_top_data()
    
    song_entry = {
        "title": song_info.get("title", "Unknown"),
        "id": song_info.get("id", ""),
        "added_by": str(user_id)
    }
    
    # Add to end
    data["global_top"].append(song_entry)
    
    # Keep only last 20
    if len(data["global_top"]) > 20:
        data["global_top"] = data["global_top"][-20:]
    
    save_top_data(data)

def remove_from_user_top(user_id, index):
    """Remove song from user's top by index (0-based)"""
    data = load_top_data()
    user_id_str = str(user_id)
    
    if user_id_str not in data["user_tops"]:
        return False, "No tienes canciones en tu top."
    
    if index < 0 or index >= len(data["user_tops"][user_id_str]):
        return False, f"Índice {index+1} fuera de rango."
    
    removed = data["user_tops"][user_id_str].pop(index)
    save_top_data(data)
    return True, f"Eliminado: {removed['title']}"

def get_random_top_song(vc):
    """Get random song from user tops (users in voice) or global top with weighted priority"""
    data = load_top_data()
    
    # Get users in voice channel
    users_in_voice = []
    if vc and vc.channel:
        users_in_voice = [str(member.id) for member in vc.channel.members if not member.bot]
    
    # Filter user tops for users in voice
    available_user_tops = []
    for user_id in users_in_voice:
        if user_id in data["user_tops"] and data["user_tops"][user_id]:
            available_user_tops.extend(data["user_tops"][user_id])
    
    # Weighted selection: prioritize connected users' tops
    import random
    
    if available_user_tops and data["global_top"]:
        # 70% from user tops, 30% from global
        if random.random() < 0.7:
            song = random.choice(available_user_tops)
            logging.info(f"📻 Seleccionado del top de usuarios conectados: {song['title']}")
        else:
            song = random.choice(data["global_top"])
            logging.info(f"🌍 Seleccionado del top global: {song['title']}")
        return song
    elif available_user_tops:
        # Solo hay tops de usuarios conectados
        song = random.choice(available_user_tops)
        logging.info(f"📻 Seleccionado del top de usuarios conectados: {song['title']}")
        return song
    elif data["global_top"]:
        # Solo hay top global
        song = random.choice(data["global_top"])
        logging.info(f"🌍 Seleccionado del top global: {song['title']}")
        return song
    
    # No hay tops disponibles
    logging.warning("⚠️ No hay tops disponibles")
    return None

class DiscordAudioPlayer:
    def __init__(self, vc, config, loop):
        self.vc = vc
        self.config = config
        self.loop = loop
        self.queue = []
        self._current_info = None
        self._current_filepath = None
        self._volume = config.get("volume", 0.02)
        self.radio_mode = True
        self.history = []
        self.history_ids = []
        self._last_query = None
        self._last_index = 0
        self._paused = False
        self._play_lock = asyncio.Lock()
        self.playback_start_time = time.time()
        self.pause_offset = 0
        self.resume_info = None # Stores (info, filepath, offset)

    @property
    def is_playing(self):
        """Sincronización real con el estado de Discord."""
        return self.vc.is_playing() if self.vc else False

    def set_volume(self, vol_percent):
        v = max(0.0, min(0.1, vol_percent / 1000.0))
        self._volume = v
        if self.vc.source:
             self.vc.source.volume = v * 10
        
    async def play_next(self):
        if self._paused or not self.vc or not self.vc.is_connected():
            return

        async with self._play_lock:
            # Re-verificar tras pillar el lock (doble check)
            if self.vc.is_playing(): 
                return

            # Limpieza agresiva: borrar el archivo anterior
            if self._current_filepath and os.path.exists(self._current_filepath):
                 try: 
                     # Esperar un poco para asegurar que FFmpeg soltó el archivo (ASYNCRONO)
                     await asyncio.sleep(0.5)
                     os.remove(self._current_filepath)
                     logging.info(f"🗑️ Archivo temporal eliminado: {self._current_filepath}")
                 except Exception as e:
                     logging.warning(f"⚠️ No se pudo borrar {self._current_filepath}: {e}")


            if self.queue:
                item = self.queue.pop(0)
                # Handle new format: (info, filepath, user_id)
                if isinstance(item, tuple) and len(item) == 3:
                    info, filepath, user_id = item
                elif isinstance(item, tuple) and len(item) == 2:
                    info, filepath = item
                    user_id = None
                else:
                    info = item
                    filepath = None
                    user_id = None

                if not filepath:
                    logging.info(f"⌛ Descargando (Lazy): {info.get('title')}")
                    url = info.get("page_url") or info.get("url") or info.get("webpage_url")
                    if not url and info.get("id"): url = f"https://www.youtube.com/watch?v={info['id']}"
                    
                    filepath = await asyncio.to_thread(download_media, url, DISCORD_PREFIX)
                    if not filepath:
                        logging.error(f"❌ Error descargando {info.get('title')}, saltando al siguiente...")
                        # IMPORTANTE: Liberamos el lock antes de la recursión para evitar interbloqueos
                        # Pero como usamos 'async with', la recursión debe ser EXTERNA o cuidadosa.
                        # Para evitar recursión infinita pegada al mismo lock, llamamos a play_next fuera del lock
                        # o simplemente volvemos a intentar dentro del mismo bucle (mejorado abajo).
                        pass # El bucle while sería ideal, pero vamos a usar una salida limpia
                    
                if not filepath:
                    # Si falló la descarga, salimos y que el siguiente tick o comando re-intente
                    # o llamamos a nosotros mismos sin el lock.
                    self.loop.create_task(self.play_next())
                    return
            elif self.radio_mode:
                # Radio mode: buscar siguiente canción
                if self._current_info:
                    # Si hay canción actual, buscar relacionadas
                    logging.info("📻 Radio: Buscando siguiente recomendación...")
                    info, filepath = await self._get_next_radio_song()
                    user_id = None  # Radio mode, no specific user
                else:
                    # No hay canción actual, buscar del top
                    logging.info("📻 Radio: Buscando canción del top para empezar...")
                    top_song = get_random_top_song(self.vc)
                    if top_song:
                        logging.info(f"📻 Radio: Seleccionada del top: {top_song['title']}")
                        # Buscar info completa usando el ID
                        info = await asyncio.to_thread(get_search_info, f"https://www.youtube.com/watch?v={top_song['id']}", 0)
                        if info:
                            url = info.get("page_url") or info.get("url") or info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}"
                            filepath = await asyncio.to_thread(download_media, url, DISCORD_PREFIX)
                            user_id = None
                        else:
                            info, filepath = None, None
                    else:
                        # No hay top, buscar algo genérico
                        logging.info("📻 Radio: No hay top disponible, buscando música popular...")
                        info = await asyncio.to_thread(get_search_info, "popular music 2024", 0)
                        if info:
                            url = info.get("page_url") or info.get("url") or info.get("webpage_url") or f"https://www.youtube.com/watch?v={info['id']}"
                            filepath = await asyncio.to_thread(download_media, url, DISCORD_PREFIX)
                            user_id = None
                        else:
                            info, filepath = None, None
            else:
                self._current_info = None
                self._current_filepath = None
                logging.info("⏹️ Fin de la cola.")
                await self._reprime_listener()
                return

            if not info or not filepath:
                return

            self._current_info = info
            self._current_filepath = filepath
            self.history.append(normalize_title(info["title"]))
            if info.get("id"): self.history_ids.append(info["id"])
            
            # Add to tops if user_id is available
            if user_id:
                try:
                    add_to_user_top(user_id, info)
                    add_to_global_top(info, user_id)
                    logging.info(f"⭐ Agregado a tops: {info.get('title')} (user: {user_id})")
                except Exception as e:
                    logging.error(f"❌ Error agregando a tops: {e}")
            
            # Usar la función centralizada de inicio
            await self._start_playback(info, filepath)
            ntitle = normalize_title(info["title"])
            if ntitle not in self.history: self.history.append(ntitle)
            
            logging.info(f"▶️ Reproduciendo en Discord: {info['title']}")
            
            await self._reprime_listener()

    async def _reprime_listener(self):
        """Asegura que el receptor de voz esté activo y su loop funcionando."""
        if self.config.get("listen_enabled", False):
            sink = recording_tasks.get(self.vc.guild.id)
            if sink:
                try:
                    # REPARACIÓN: Si el loop de procesamiento se murió, reiniciarlo
                    if hasattr(sink.process_audio, 'is_running') and not sink.process_audio.is_running():
                        logging.warning("⚠️ [Fix] El loop de audio del Sink estaba detenido. Reiniciando...")
                        sink.process_audio.start()
                    
                    # Siempre re-enganchar el oído por si Discord lo soltó
                    if not self.vc.is_listening() or not sink.is_currently_listening:
                        self.vc.listen(sink)
                        sink.is_currently_listening = True
                        logging.info("👂 [Fix] Oído re-enganchado. El bot está listo para escucharte.")
                except Exception as e:
                    logging.warning(f"⚠️ [Fix] Error re-enganchando oído: {e}")
            else:
                logging.info("👂 [Fix] No había sink activo para re-enganchar.")

    async def _get_next_radio_song(self):
        # Mantenimiento de historial: solo guardar últimas 100
        if len(self.history) > 100: self.history = self.history[-100:]
        if len(self.history_ids) > 100: self.history_ids = self.history_ids[-100:]

        current_id = self._current_info.get("id") if self._current_info else None
        current_norm = normalize_title(self._current_info["title"]) if self._current_info else ""

        # Intento 1: Recomendaciones basadas en canción actual (Directas + Búsqueda Relacionada)
        if current_id:
            logging.debug(f"📻 [Radio] Buscando recomendaciones para {current_id}...")
            recs = await asyncio.to_thread(get_recommendations, current_id, self._current_info.get("title"))
            found = await self._find_first_valid(recs, current_id, current_norm)
            if found: return found

        # Intento 2: Paginación de la última búsqueda (si existe)
        if self._last_query:
            logging.debug(f"📻 [Radio] Buscando en profundidad resultados de '{self._last_query}'...")
            for i in range(1, 15):
                idx = self._last_index + i
                info = await asyncio.to_thread(get_search_info, self._last_query, idx)
                if not info: continue
                # Validar
                valid, fpath = await self._validate_and_download_radio(info, current_id, current_norm)
                if valid:
                    self._last_index = idx
                    return info, fpath

        # Intento 3: EXPLORACIÓN - Basada en 'forced_keyword' (si existe)
        fk = self.config.get("forced_keyword")
        if fk:
            offset = random.randint(1, 30)
            logging.info(f"📻 [Radio] Intentando descubrimiento por forzado '{fk}' (offset {offset})...")
            info = await asyncio.to_thread(get_search_info, fk, offset)
            if info:
                valid, fpath = await self._validate_and_download_radio(info, current_id, current_norm)
                if valid: return info, fpath

        # Intento 4: RAMIFICACIÓN - Basada en una canción aleatoria de la historia reciente
        if len(self.history_ids) > 1:
            prev_id = random.choice(self.history_ids[-15:-1])
            logging.info(f"📻 [Radio] Rama muerta. Saltando a recomendaciones de historia previa ({prev_id})... gateway a lo 'similar'")
            recs = await asyncio.to_thread(get_recommendations, prev_id)
            found = await self._find_first_valid(recs, current_id, current_norm)
            if found: return found

        # Intento 5: Fallback de emergencia - Limpiar historial parcial
        if self.history:
            logging.warning("⚠️ Radio: No encuentro más canciones nuevas. Limpiando historial antiguo.")
            self.history = self.history[-10:]
            self.history_ids = self.history_ids[-10:]
            base_query = self.config.get("forced_keyword") or self._last_query or "pop hits 2024"
            info = await asyncio.to_thread(get_search_info, base_query, random.randint(0, 20))
            if info:
                valid, fpath = await self._validate_and_download_radio(info, current_id, current_norm)
                if valid: return info, fpath
        
        return None, None

    async def _find_first_valid(self, entries, current_id, current_norm):
        for entry in entries:
            valid, fpath = await self._validate_and_download_radio(entry, current_id, current_norm)
            if valid:
                full_info = await asyncio.to_thread(get_search_info, entry.get("id"), 0)
                return full_info or entry, fpath
        return None

    async def _validate_and_download_radio(self, entry, current_id, current_norm):
        eid = entry.get("id")
        if not eid: return False, None
        if eid == current_id or eid in self.history_ids: return False, None
        ntitle = normalize_title(entry.get("title", ""))
        if ntitle in self.history or (current_norm and ntitle == current_norm):
            return False, None
        if not is_content_allowed(entry, self.config): return False, None
        url = entry.get("page_url") or entry.get("url") or f"https://www.youtube.com/watch?v={eid}"
        fpath = await asyncio.to_thread(download_media, url, DISCORD_PREFIX)
        if fpath: return True, fpath
        return False, None

    async def play_query(self, query, preempt=False, force_playlist=None, user_id=None):
        if not discord.opus.is_loaded():
            return "❌ Opus no está cargado."

        is_pl_cmd = force_playlist or "playlist" in query.lower() or "lista" in query.lower()
        logging.info(f"🔎 Buscando: {query} (Playlist: {is_pl_cmd})")
        try:
            # Detectar playlist si no se fuerza
            is_pl = force_playlist if force_playlist is not None else ("list=" in query or is_pl_cmd)
            info = await asyncio.to_thread(get_search_info, query, 0, is_playlist=is_pl)
            
            if not info: return "❌ No encontré nada."
            
            if info.get('_type') == 'playlist':
                entries = info.get('entries', [])
                if not entries: return "❌ Playlist vacía o inaccesible."
                
                logging.info(f"📂 Procesando playlist '{info.get('title')}' con {len(entries)} temas.")
                added = 0
                for i, entry in enumerate(entries):
                    if is_content_allowed(entry, self.config):
                        # Store as (info, None for filepath, user_id)
                        queue_item = (entry, None, user_id)
                        if preempt and i == 0:
                            self.queue.insert(0, queue_item)
                        else:
                            self.queue.append(queue_item)
                        added += 1
                
                if added == 0: return "🛡️ Playlist bloqueada por filtros de contenido."
                
                msg = f"✅ Añadida playlist: **{info.get('title', 'Desconocida')}** ({added} temas)."
                if preempt or not self.is_playing:
                    if preempt and self.vc.is_playing(): self.vc.stop()
                    asyncio.create_task(self.play_next())
                return msg

            # Si llegamos aquí y el usuario pidió playlist específicamente con list=, es que falló la extracción
            if "list=" in query:
                logging.warning(f"⚠️ Se detectó 'list=' pero yt-dlp no devolvió una playlist para: {query}")

            # Si es video único
            if not is_content_allowed(info, self.config): return "🛡️ Contenido bloqueado."

            # Store as (info, None for filepath, user_id)
            queue_item = (info, None, user_id)
            if preempt:
                self.queue.insert(0, queue_item)
                if self.vc.is_playing(): 
                    self.vc.stop()
                else:
                    asyncio.create_task(self.play_next())
                return f"▶️ Iniciando: **{info['title']}**"
            else:
                if self.is_playing:
                    self.queue.append(queue_item)
                    return f"⌛ Añadido a la cola: **{info['title']}**"
                else:
                    self.queue.append(queue_item)
                    asyncio.create_task(self.play_next())
                    return f"▶️ Iniciando: **{info['title']}**"

        except Exception as e:
            logging.error(f"Error en play_query: {e}")
            return f"❌ Error: {e}"

    async def pause_and_store(self):
        if self.vc and self.vc.is_playing():
            # Fade out
            if self.vc.source:
                original_vol = getattr(self.vc.source, 'volume', self._volume * 10)
                for i in range(10, -1, -1):
                    try: self.vc.source.volume = (original_vol * i) / 10
                    except: pass
                    await asyncio.sleep(0.05)

            # Calcular offset actual
            elapsed = time.time() - self.playback_start_time
            if elapsed < 0: elapsed = 0
            self.pause_offset += elapsed
            
            # IMPORTANTE: Detener en lugar de pausar para permitir vc.play del bot
            # Y marcar como pausado para que play_next no salte solo
            self._paused = True
            self.vc.stop()
            
            # RE-PRIMAR inmediatamente para no perder el comando de voz
            await self._reprime_listener()
            
            self.resume_info = (self._current_info, self._current_filepath, self.pause_offset)
            logging.info(f"⏸️ Música detenida y guardada en {self.pause_offset:.2f}s")
            return True
        return False

    async def resume_from_stored(self):
        if self.resume_info:
            self._paused = False # Permitir reproducción de nuevo
            info, filepath, offset = self.resume_info
            self.resume_info = None
            if not info: return False
            title = info.get('title', 'Canción anterior')
            logging.info(f"⏯️ Reanudando {title} desde {offset:.2f}s...")
            # Re-iniciar playback con -ss offset
            await self._start_playback(info, filepath, offset=offset)
            
            # Fade in (se hace dentro de _start_playback o aquí)
            # Como _start_playback crea una nueva source, lo hacemos aquí después de vc.play
            # Pero vc.play ocurre DENTRO de _start_playback.
            # Mejor dejar que _start_playback maneje el volumen inicial.
            return True
        return False

    async def _start_playback(self, info, filepath, offset=0):
        # SI HAY UNA PETICIÓN DE VOZ ACTIVA, NO INICIAMOS AHORA
        # Guardamos en resume_info para que se inicie al cerrar la ventana (después del PLIM_OK)
        sink = recording_tasks.get(self.vc.guild.id)
        if sink and sink.active_requests:
            # Si ya hay un offset (resumen), lo respetamos. Si es nuevo play, offset 0.
            self.resume_info = (info, filepath, offset)
            logging.info(f"⏳ Postponiendo inicio de {info.get('title', '???')} hasta fin de comando.")
            return

        # Limpiar cualquier audio previo
        if self.vc and self.vc.is_playing():
             self.vc.stop()
        
        self._current_info = info
        self._current_filepath = filepath
        self.playback_start_time = time.time()
        self.pause_offset = offset
        
        ffmpeg_opts = f"-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 2 -loglevel info"
        if offset > 0:
            # -ss antes de -i es más rápido
            ffmpeg_opts = f"-ss {offset} " + ffmpeg_opts

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(filepath, executable=FFMPEG_PATH, options=ffmpeg_opts), 
                volume=self._volume * 10
            )
        except Exception as e:
            logging.error(f"Error creando FFmpegPCMAudio: {e}")
            return

        def after_playing(error):
            if error: logging.error(f"Error en reproducción: {error}")
            asyncio.run_coroutine_threadsafe(self.play_next(), self.loop)

        if self.vc and self.vc.is_connected():
            title = info.get('title', '???')
            self.history.append(normalize_title(title))
            if info.get("id"): self.history_ids.append(info["id"])
            self.vc.play(source, after=after_playing)
            
            # Fade in obligatorio para evitar el spike (estallido) inicial
            asyncio.run_coroutine_threadsafe(self._fade_in(source), self.loop)
            
            # RE-PRIMAR el oído aquí también para evitar "sordera" tras play()
            asyncio.run_coroutine_threadsafe(self._reprime_listener(), self.loop)
        else:
            pass

    async def _fade_in(self, source):
        target_vol = source.volume
        source.volume = 0
        for i in range(1, 11):
            await asyncio.sleep(0.05)
            source.volume = (target_vol * i) / 10

# --- Bot de Discord ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True, help_command=None)

players = {} 
recording_tasks = {} 

@bot.event
async def on_ready():
    logging.info(f"✅ Bot conectado como {bot.user}")

@bot.event
async def on_command(ctx):
    logging.info(f"💬 Comando recibido: {ctx.command.name} de {ctx.author.name}")

@bot.event
async def on_command_error(ctx, error):
    logging.error(f"❌ Error en comando {ctx.command}: {error}")

@bot.event
async def on_message(message):
    # Ignorar mensajes del bot
    if message.author == bot.user:
        return
    
    # Procesar comandos normales primero (para !join, etc.)
    await bot.process_commands(message)
    
    # Detectar comandos con "rafa" como prefijo
    content = message.content.strip()
    if content.lower().startswith("rafa "):
        # Extraer el comando después de "rafa "
        command_text = content[5:].strip()
        
        if not command_text:
            return
        
        # Verificar que hay un player activo
        player = players.get(message.guild.id)
        if not player:
            # Si no hay player pero es comando join, permitir
            if command_text.lower() in ["join", "únete", "conecta"]:
                await bot.process_commands(message)
                return
            await message.channel.send("❌ El bot no está conectado. Usa `!join` primero.")
            return
        
        # Crear un contexto simulado para handle_voice_command
        ctx = await bot.get_context(message)
        
        # Pasar el comando al handler unificado
        await handle_voice_command(ctx, player, command_text)

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        logging.info(f"🔊 Intentando conectar a '{channel.name}' (ID: {channel.id})...")
        try:
            # Cargar config de Discord fresca/actual
            d_config = load_discord_config()
            
            # Aumentar timeout y usar self_deaf para estabilidad
            # Si ya hay un voice_client pero no está conectado o está en estado incierto, limpiar
            if ctx.voice_client:
                if not ctx.voice_client.is_connected():
                    logging.info("🧹 Limpiando conexión previa inactiva...")
                    try:
                        await ctx.voice_client.disconnect(force=True)
                        await asyncio.sleep(1)
                    except: pass
            
            if not ctx.voice_client:
                logging.info("📞 Iniciando conexión (handshake)...")
                # IMPORTANTE: Usar cls=voice_recv.VoiceRecvClient para permitir escucha
                vc = await channel.connect(timeout=60.0, cls=voice_recv.VoiceRecvClient)
                logging.info("🤝 Handshake inicial completado.")
                
                # Esperar a que el estado sea 'is_connected' de verdad
                for i in range(15):
                    if vc.is_connected():
                        logging.info(f"✅ Conexión confirmada tras {i*0.5}s.")
                        break
                    await asyncio.sleep(0.5)
                
                if not vc.is_connected():
                    logging.warning("⚠️ El Handshake terminó pero is_connected() sigue siendo False.")
            else:
                vc = ctx.voice_client
                if vc.channel != channel:
                    logging.info(f"🚚 Moviendo bot de {vc.channel.name} a {channel.name}...")
                    await vc.move_to(channel)
                    logging.info("🚚 Movido con éxito.")
            
            # Primero instanciamos el player con la config de DISCORD
            players[ctx.guild.id] = DiscordAudioPlayer(vc, d_config, bot.loop)
            await ctx.send(f"✅ Conectado a **{channel.name}**")
            
            # Auto-arrancar escucha si está activada en config
            if d_config.get("listen_enabled", False):
                await start_listening(ctx, d_config.get("hotwords", ["rafa"]), d_config.get("activation_prefixes", ["oye"]))

        except asyncio.TimeoutError:
            logging.error("❌ Error: Tiempo de espera agotado al conectar al canal de voz.")
            await ctx.send("❌ No pude conectarme a tiempo. ¿Está el canal saturado o hay problemas de red?")
        except Exception as e:
            logging.error(f"❌ Error al conectar: {e}")
            logging.error(traceback.format_exc())
            await ctx.send(f"❌ Error al conectar: {e}")
    else:
        await ctx.send("❌ Debes estar en un canal de voz.")

@bot.command()
async def play(ctx, *, query):
    if not ctx.voice_client:
        await join(ctx)
    
    if not ctx.voice_client:
        return

    player = players.get(ctx.guild.id)
    if not player:
        players[ctx.guild.id] = DiscordAudioPlayer(ctx.voice_client, load_discord_config(), bot.loop)
        player = players[ctx.guild.id]
    else:
        player.vc = ctx.voice_client

    # Use unified voice command handler
    await handle_voice_command(ctx, player, f"pon {query}")

@bot.command()
async def stop(ctx):
    player = players.get(ctx.guild.id)
    if not player:
        return await ctx.send("❌ No estoy conectado.")
    await handle_voice_command(ctx, player, "stop")

@bot.command()
async def skip(ctx):
    player = players.get(ctx.guild.id)
    if not player:
        return await ctx.send("❌ No estoy conectado.")
    await handle_voice_command(ctx, player, "siguiente")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await stop_listening(ctx) # Limpieza total del oído
        await ctx.voice_client.disconnect()
        players.pop(ctx.guild.id, None)
        await ctx.send("👋 Adiós.")

# --- Bucle de Voz (RESTORED via discord-ext-voice-recv) ---
class VoiceRecognitionSink(voice_recv.AudioSink):
    def __init__(self, hotwords, prefixes, ctx, player):
        super().__init__()
        # hotwords y prefixes pueden ser lista o str
        if isinstance(hotwords, str): self.hotwords = [hotwords.lower()]
        else: self.hotwords = [h.lower() for h in hotwords]
        
        if isinstance(prefixes, str): self.prefixes = [prefixes.lower()]
        else: self.prefixes = [p.lower() for p in (prefixes or ["oye"])]
        
        self.ctx = ctx
        self.player = player
        self.recognizer = sr.Recognizer()
        self.user_buffers = {} # Dict: user_id -> BytesIO (Command window)
        self.sliding_windows = {} # Dict: user_id -> bytearray (4s sliding window)
        self.active_requests = {} # Dict: user_id -> {'start_time': float, 'name': str, 'task': asyncio.Task}
        self.recognizing_uids = set() # Track users currently in _check_for_activation
        self.lock = threading.Lock()
        self.packet_count = 0
        self.last_packet_count = 0
        self.inactive_seconds = 0
        self.is_currently_listening = False # Flag de estado real

    def wants_opus(self) -> bool:
        return False  # Queremos PCM descodificado

    def write(self, user, data):
        # Acumular data por usuario con lock
        if user is None: return
        with self.lock:
            # 1. Buffer de comando (si está en ventana de 7s)
            if user.id in self.active_requests:
                if user.id not in self.user_buffers:
                    self.user_buffers[user.id] = io.BytesIO()
                self.user_buffers[user.id].write(data.pcm)
            
            # 2. Ventana deslizante (siempre activa para detección de hotword)
            if user.id not in self.sliding_windows:
                self.sliding_windows[user.id] = bytearray()
            
            self.sliding_windows[user.id].extend(data.pcm)
            
            # Mantener máximo 12 segundos de audio para capturar contexto (48000 Hz * 2 canales * 2 bytes * 12 s = 2304000 bytes)
            # Esto permite capturar frases largas como "jajajaja oye rafa vale"
            MAX_SLIDING_BYTES = 2304000
            if len(self.sliding_windows[user.id]) > MAX_SLIDING_BYTES:
                self.sliding_windows[user.id] = self.sliding_windows[user.id][-MAX_SLIDING_BYTES:]

            self.packet_count += 1
            if self.packet_count == 1:
                logging.info("🎤 [Sink] Primer paquete de audio recibido. ¡Discord está enviando datos!")
            elif self.packet_count % 1000 == 0:
                # Log cada 1000 paquetes para confirmar que sigue recibiendo
                logging.info(f"📊 [Sink] Recibidos {self.packet_count} paquetes de audio (bot escuchando activamente)")

    @tasks.loop(seconds=2.0)
    async def process_audio(self):
        # Procesar ventanas deslizantes para detección de hotword
        user_windows_to_process = {}
        with self.lock:
            for uid, window in self.sliding_windows.items():
                # NO procesar si ya hay una petición activa O si ya se está reconociendo ese usuario
                if uid not in self.active_requests and uid not in self.recognizing_uids and len(window) > 0:
                    user_windows_to_process[uid] = bytes(window)
        
        if not user_windows_to_process:
            self.inactive_seconds += 1
            if self.inactive_seconds % 30 == 0:
                logging.info(f"💓 [Sink Heatbeat] Loop activo. Paquetes totales: {self.packet_count}")

            if self.packet_count > self.last_packet_count:
                self.inactive_seconds = 0
                self.last_packet_count = self.packet_count
            
            if self.inactive_seconds >= 20 and self.player.config.get("listen_enabled", False) and self.packet_count > 0:
                if self.player.vc and not self.player.vc.is_listening():
                     try:
                         self.player.vc.listen(self)
                         logging.info("🐕 [Watchdog] Oído re-enganchado.")
                     except: pass
                self.inactive_seconds = 0
            
            # LIMPIEZA PERIÓDICA: Cada 60 segundos, limpiar buffers de usuarios desconectados
            if self.inactive_seconds % 60 == 0:
                asyncio.create_task(self._cleanup_inactive_users())
            
            return

        for uid, audio_bytes in user_windows_to_process.items():
            # Procesar para detectar "Oye Rafael" en la ventana deslizante
            asyncio.create_task(self._check_for_activation(uid, audio_bytes))
    
    async def _cleanup_inactive_users(self):
        """Limpia buffers de usuarios que ya no están en el canal de voz"""
        try:
            # Obtener usuarios actualmente en el canal
            active_users = set()
            if self.player.vc and self.player.vc.channel:
                active_users = {member.id for member in self.player.vc.channel.members if not member.bot}
            
            # Limpiar buffers de usuarios inactivos
            cleaned = 0
            with self.lock:
                inactive_users = set(self.sliding_windows.keys()) - active_users
                for uid in inactive_users:
                    if uid in self.sliding_windows:
                        del self.sliding_windows[uid]
                        cleaned += 1
                    if uid in self.user_buffers:
                        del self.user_buffers[uid]
            
            if cleaned > 0:
                logging.info(f"🧹 Limpieza automática: {cleaned} usuario(s) desconectado(s) eliminados de buffers")
        except Exception as e:
            logging.error(f"❌ Error en _cleanup_inactive_users: {e}")

    async def _check_for_activation(self, uid, audio_bytes):
        # Evitar re-entrada y aplicar COOLDOWN (ej: 5 segundos) para no detectar lo mismo varias veces
        if uid in self.recognizing_uids: return
        
        last_act = getattr(self, f"_last_activation_{uid}", 0)
        if time.time() - last_act < 5.0: return # Cooldown de 5s

        self.recognizing_uids.add(uid)
        try:
            # Convertir a mono y DOWN-SAMPLE a 16000Hz (Google lo prefiere y es 3x más rápido)
            pcm_array = array.array('h', audio_bytes)
            mono_array = pcm_array[::2]      # Stereo -> Mono
            down_array = mono_array[::3]      # 48000Hz -> 16000Hz (6x menos datos totales)
            mono_bytes = down_array.tobytes()
            audio = sr.AudioData(mono_bytes, 16000, 2)
            
            text = await asyncio.to_thread(self._recognize, audio)
            
            # DIAGNÓSTICO: Mostrar cuánto audio (en segundos) estamos enviando a Google
            audio_duration = len(audio_bytes) / (48000 * 2 * 2) # segundos
            logging.info(f"🔍 [Check Activation] Procesando {audio_duration:.1f}s de audio para {uid}...")

            if not text: 
                return

            # Tarea: imprimir en consola lo que van diciendo los usuarios
            user_name = "Desconocido"
            member = self.ctx.guild.get_member(uid)
            if member: user_name = member.display_name
            
            # Solo loguear si hay texto y es distinto al anterior para no "rebotar"
            last_text = getattr(self, f"_last_text_{uid}", "").strip().lower()
            current_text = text.strip().lower()
            if current_text != last_text and len(current_text) > 2:
                print(f"👂 [Escuchado ({user_name})]: '{text}'")
                setattr(self, f"_last_text_{uid}", current_text)

            text_lower = text.lower()
            # Patrón: Prefijo + uno de los hotwords o misinterpretaciones comunes
            activation_detected = False
            
            # Verificar si hay algún prefijo (oye, hey, a ver, etc.)
            detected_prefix = None
            for p in self.prefixes:
                if p in text_lower:
                    detected_prefix = p
                    break
            
            has_prefix = detected_prefix is not None

            for hw in self.hotwords:
                target = hw.lower()
                # Coincidencia directa con prefijo: "oye rafa", "hey rafa"
                if has_prefix and (f"{detected_prefix} {target}" in text_lower or (target in text_lower and detected_prefix in text_lower)):
                    activation_detected = True
                    break
                
                # Coincidencia fonética (Solo si se detectó un prefijo para evitar falsos positivos)
                if has_prefix and target == "rafa":
                    if any(alias in text_lower for alias in ["gafa", "capa", "papa", "claro", "perfecto", "chicas", "rafa"]):
                        logging.info(f"🤔 [Fuzzy Match] Detectado '{text}' como activación de '{target}' con prefijo '{detected_prefix}'")
                        activation_detected = True
                        break
                
                # Coincidencia directa SIN prefijo (opcional, pero permitimos si es exacto)
                if target in text_lower and len(text_lower.split()) < 3: # "Rafa", "Oye Rafa"
                    activation_detected = True
                    break
            
            if activation_detected:
                # Limpiamos el buffer y la ventana DESLIZANTE para no repetir detección
                # LO HACEMOS ANTES DE CUALQUIER AWAIT para evitar race conditions
                with self.lock:
                    self.sliding_windows[uid] = bytearray()
                    self.user_buffers[uid] = io.BytesIO()
                    setattr(self, f"_last_text_{uid}", "") # Reset log cache

                # Pausar música si está sonando
                await self.player.pause_and_store()

                # Obtener nombre del usuario (redundancia tras limpieza)
                user_name = "usuario"
                member = self.ctx.guild.get_member(uid)
                if member: user_name = member.display_name

                # Registrar activación
                self.active_requests[uid] = {'start_time': time.time(), 'name': user_name}
                setattr(self, f"_last_activation_{uid}", time.time()) # Iniciar cooldown
                
                print(f"✨ [Activación] Hotword detectado para {user_name}. ¡Dime!")
                logging.info(f"✨ [Activación] Hotword detectado para user {uid} ({user_name})")

                # SEED COMMAND BUFFER: Empezar con lo que ya tenemos en la ventana deslizante
                # Capturamos el contexto ANTES de limpiar para no perder ni un milisegundo
                context_bytes = bytes(self.sliding_windows.get(uid, bytearray()))
                with self.lock:
                    # Inicializar buffer con context y POSICIONAR AL FINAL para que write() anexe
                    self.user_buffers[uid] = io.BytesIO(context_bytes)
                    self.user_buffers[uid].seek(0, io.SEEK_END)
                    self.sliding_windows[uid] = bytearray() # Ahora sí limpiamos la ventana
                    
                logging.info(f"⚡ [Optimización] Buffer de comando pre-cargado con context ({len(context_bytes)} bytes)")

                # Feedback: "*PLIM* Dime {user_name}"
                await self._play_confirmation(user_name)
                
                # Ya estamos capturando desde el hotword y write() está funcionando en background
                logging.info(f"🎙️ Escuchando comando continuo de user {uid}...")
                
                # RE-PRIMAR el oído inmediatamente tras hablar para no perder el comando
                await self.player._reprime_listener()
                
                # Esperar 5.0 segundos para el comando (Damos más tiempo al usuario)
                logging.info(f"⏳ Esperando 5.0 segundos para capturar comando de user {uid}...")
                await asyncio.sleep(5.0)
                
                # Verificar cuánto audio se capturó
                with self.lock:
                    captured_size = len(self.user_buffers.get(uid, io.BytesIO()).getvalue())
                logging.info(f"📊 Audio capturado: {captured_size} bytes en 5.0 segundos para user {uid}")
                
                # Procesar lo acumulado en esos 5s
                await self._process_command_window(uid)
            else:
                # REPARACIÓN: Si no hay activación, limpiamos un poco el buffer deslizante
                # para que no se reconozca lo mismo en el siguiente tick (evita repeticiones)
                with self.lock:
                    if uid in self.sliding_windows:
                        # REPARACIÓN: Mantener 10 segundos de historia (48000 * 2 canales * 2 bytes * 10s)
                        # Esto evita que una frase en curso se corte por la mitad entre ticks de 2s.
                        MAX_KEEP_BYTES = 1920000 
                        window = self.sliding_windows[uid]
                        if len(window) > MAX_KEEP_BYTES:
                            self.sliding_windows[uid] = window[-MAX_KEEP_BYTES:]
                        logging.info(f"📚 [History] Manteniendo {len(self.sliding_windows[uid]) / 192000:.1f}s de audio previo para {uid}")
        except Exception as e:
            logging.error(f"❌ Error en _check_for_activation: {e}")
        finally:
            if uid in self.recognizing_uids: self.recognizing_uids.remove(uid)

    async def _play_confirmation(self, name):
        try:
            text = f"Dime {name}"
            # Generar audio con pyttsx3 a archivo temporal
            def _gen():
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    path = f.name
                engine = pyttsx3.init()
                engine.setProperty('rate', 150)
                engine.save_to_file(text, path)
                engine.runAndWait()
                return path
            
            tts_path = await asyncio.to_thread(_gen)
            
            # Combinar PLIM.mp3 (local) con el TTS generado usando ffmpeg (concatenar)
            # Creamos un archivo de lista para ffmpeg concat
            plim_path = os.path.join(_SCRIPT_DIR, "PLIM.mp3")
            

            
            # Reproducir en Discord: Primero el PLIM, luego el TTS (más rápido que concatenar con ffmpeg)
            if self.player.vc and self.player.vc.is_connected():
                # Si por algún motivo algo se coló, detenerlo
                if self.player.vc.is_playing(): self.player.vc.stop()
                
                # Reproducir PLIM
                self.player.vc.play(discord.FFmpegPCMAudio(plim_path, executable=FFMPEG_PATH))
                
                # Esperar lo justo para el PLIM (~0.6s) y luego poner el TTS
                await asyncio.sleep(0.6)
                if self.player.vc and self.player.vc.is_connected():
                    self.player.vc.play(discord.FFmpegPCMAudio(tts_path, executable=FFMPEG_PATH))
                
                # Esperar a que termine de hablar el TTS (~1s) - optimizado
                await asyncio.sleep(1.2)
                
                # Borrar archivo temporal
                try: os.remove(tts_path)
                except: pass


        except Exception as e:
            logging.error(f"❌ Error en _play_confirmation: {e}")
            logging.error(traceback.format_exc())

    async def _play_simple_audio(self, filename):
        """Reproduce un sonido simple (ej: PLIM_OK.mp3) sin TTS."""
        try:
            path = os.path.join(_SCRIPT_DIR, filename)
            if not os.path.exists(path):
                logging.warning(f"⚠️ Archivo no encontrado para audio simple: {path}")
                return

            if self.player.vc and self.player.vc.is_connected():
                if self.player.vc.is_playing(): self.player.vc.stop()
                self.player.vc.play(discord.FFmpegPCMAudio(path, executable=FFMPEG_PATH))
                # Esperar un poco a que el sonido se reproduzca (suelen ser < 1s)
                await asyncio.sleep(1.0)
        except Exception as e:
            logging.error(f"❌ Error en _play_simple_audio({filename}): {e}")

    async def _process_command_window(self, uid):
        try:
            audio_bytes = None
            with self.lock:
                if uid in self.user_buffers:
                    buf = self.user_buffers[uid]
                    buf.seek(0)
                    audio_bytes = buf.read()
                    self.user_buffers[uid] = io.BytesIO() # Reset
            
            if not audio_bytes or len(audio_bytes) < 1000:
                logging.warning(f"⚠️ Ventana de 5.0s cerrada para {uid} (timeout/sin audio).")
                # LIMPIAR PETICIÓN ANTES DE REANUDAR
                if uid in self.active_requests: del self.active_requests[uid]
                
                await self._play_simple_audio("PLIM_NO.mp3")
                await self.player.resume_from_stored()
                return

            logging.info(f"🎙️ Procesando ventana de comando (5.0s) para user_id {uid} ({len(audio_bytes)} bytes)")
            
            # DOWN-SAMPLING CRÍTICO: 48kHz Stereo -> 16kHz Mono (Igual que en hotword)
            # Esto mejora DRAMÁTICAMENTE la fiabilidad de Google Speech
            pcm_array = array.array('h', audio_bytes)
            mono_array = pcm_array[::2]      # Stereo -> Mono
            down_array = mono_array[::3]      # 48000Hz -> 16000Hz
            mono_bytes = down_array.tobytes()
            
            logging.info(f"🔊 Audio optimizado: {len(audio_bytes)} bytes (48k stereo) -> {len(mono_bytes)} bytes (16k mono)")
            
            audio = sr.AudioData(mono_bytes, 16000, 2)
            
            logging.info(f"🎤 Enviando a Google Speech Recognition (Optimizado 16kHz)...")
            command_text = await asyncio.to_thread(self._recognize, audio)
            
            if command_text:
                logging.info(f"✅ Reconocimiento exitoso: '{command_text}'")
                print(f"🗣️ [Comando Transcrito]: '{command_text}'")
                
                text_clean = command_text.lower().strip()
                if text_clean in ["nada", "cierra", "cancelar", "nada rafa", "nada rafael"]:
                    logging.info(f"🚫 Usuario {uid} canceló con '{command_text}'.")
                    await self._play_simple_audio("PLIM_NO.mp3")
                else:
                    # Intentar ejecutar comando
                    logging.info(f"⚙️ Ejecutando comando interpretado: '{command_text}'")
                    success = await handle_voice_command(self.ctx, self.player, command_text)
                    
                    # LIMPIAR PETICIÓN ANTES DE Feedback/Reanudación
                    if uid in self.active_requests: del self.active_requests[uid]
                    
                    if success:
                        await self._play_simple_audio("PLIM_OK.mp3")
                    else:
                        logging.warning(f"⚠️ El comando '{command_text}' no se pudo ejecutar o no es válido.")
                        await self._play_simple_audio("PLIM_NO.mp3")
            else:
                logging.info(f"❓ No se entendió nada en el audio de {uid} (Posible ruido o comando vacío).")
                await self._play_simple_audio("PLIM_NO.mp3")
            
            # Asegurar limpieza si no se hizo arriba (ej: en cancelación o error de reconocimiento)
            if uid in self.active_requests: del self.active_requests[uid]
            
            # LIMPIEZA AGRESIVA: Borrar completamente el sliding window para este usuario
            # Esto evita acumulación de audio viejo que degrada el reconocimiento
            with self.lock:
                if uid in self.sliding_windows:
                    self.sliding_windows[uid] = bytearray()  # Reset completo
                    logging.debug(f"🧹 Limpieza completa del buffer de audio para user {uid}")
            
            await self.player.resume_from_stored()
            
        except Exception as e:
            logging.error(f"❌ Error en _process_command_window: {e}")
            if uid in self.active_requests: del self.active_requests[uid]
            
            # Limpiar buffer incluso en error
            with self.lock:
                if uid in self.sliding_windows:
                    self.sliding_windows[uid] = bytearray()
            
            await self.player.resume_from_stored()

    def _recognize(self, audio):
        try:
            return self.recognizer.recognize_google(audio, language="es-ES")
        except sr.UnknownValueError:
            return None # No entendió nada
        except Exception as e:
            logging.error(f"❌ Error en recognize_google: {e}")
            return None
        
    def cleanup(self):
        self.process_audio.cancel()



async def handle_voice_command(ctx, player, text):
    logging.info(f"➡️ handle_voice_command invocado con: '{text}'")
    try:
        parser = CommandParser()
        cmd, args = parser.parse(text)
        logging.info(f"🧩 Parser resultado: cmd='{cmd}', args={args}")
        
        if not cmd: 
            logging.warning(f"⚠️ Comando no reconocido: '{text}'")
            return False
    
        logging.info(f"🎙️ Ejecutando comando de voz: {cmd} {args}")
    
        try:
            if cmd == "volume": 
                 player.set_volume(args["n"])
                 player.config["volume"] = player._volume
                 save_discord_config(player.config)
                 await ctx.send(f"🎙️ **Voz**: Volumen {args['n']}%")
            
            elif cmd == "volume_rel":
                current_vol_percent = int(player._volume * 1000)
                change = 10 if args["direction"] == "up" else -10
                new_vol = max(0, min(100, current_vol_percent + change))
                player.set_volume(new_vol)
                player.config["volume"] = player._volume
                save_discord_config(player.config)
                await ctx.send(f"🎙️ **Voz**: Volumen ajustado a {new_vol}% ({'⬆️' if change>0 else '⬇️'})")

            elif cmd == "mute":
                player._saved_volume = player._volume
                player.set_volume(0)
                await ctx.send("🎙️ **Voz**: 🔇 Silenciado.")

            elif cmd == "unmute":
                vol = getattr(player, '_saved_volume', 0.05)
                if vol == 0: vol = 0.05
                player.set_volume(int(vol * 1000))
                await ctx.send("🎙️ **Voz**: 🔊 Sonido restaurado.")

            elif cmd == "replay":
                if player._current_info:
                    await ctx.send(f"🎙️ **Voz**: 🔄 Repitiendo {player._current_info['title']}...")
                    await player.play_query(player._current_info['title'], user_id=ctx.author.id)
                else:
                    await ctx.send("🎙️ **Voz**: ❌ No hay nada sonando para repetir.")
    
            elif cmd == "play": 
                # Detectar palabra "playlist" o "lista" para forzar modo
                text_lower = text.lower()
                force_pl = ("playlist" in text_lower or "lista" in text_lower)
                await ctx.send(await player.play_query(args["query"], preempt=True, force_playlist=force_pl, user_id=ctx.author.id))
            elif cmd == "stop": 
                player.resume_info = None
                player._paused = False
                player.vc.stop()
                player.is_playing = False
                player.queue.clear() # Stop suele significar "para todo"
                await ctx.send("🎙️ **Voz**: Detenido y cola limpiada.")
            elif cmd == "pause": 
                if player.vc.is_playing(): 
                    player.vc.pause()
                    await ctx.send("🎙️ **Voz**: Pausado.")
            elif cmd == "resume": 
                if player.vc.is_paused(): 
                    player.vc.resume()
                    await ctx.send("🎙️ **Voz**: Reanudado.")
            elif cmd == "next":
                player.resume_info = None
                player._paused = False
                # Para saltar, paramos. El callback after_playing llamará a play_next
                if player.vc.is_playing() or player.vc.is_paused():
                    player.vc.stop() 
                    await ctx.send("🎙️ **Voz**: Siguiente...")
                else:
                    # Si no suena nada, intentamos forzar play_next
                    await player.play_next()

            elif cmd == "queue":
                if not player.queue:
                    await ctx.send("🎙️ **Voz**: La cola está vacía.")
                else:
                    msg = "📋 **Cola actual**:\n"
                    for i, item in enumerate(player.queue[:10], 1):
                        info = item[0] if isinstance(item, tuple) else item
                        msg += f"{i}. {info.get('title', '???')}\n"
                    if len(player.queue) > 10:
                        msg += f"... y {len(player.queue)-10} más."
                    await ctx.send(msg)

            elif cmd == "remove":
                idx = args["n"] - 1
                if 0 <= idx < len(player.queue):
                    item = player.queue.pop(idx)
                    info = item[0] if isinstance(item, tuple) else item
                    await ctx.send(f"🎙️ **Voz**: Eliminado: {info.get('title')}")
                else:
                    await ctx.send(f"🎙️ **Voz**: Índice {args['n']} fuera de rango.")

            elif cmd == "clear":
                player.queue.clear()
                await ctx.send("🎙️ **Voz**: Cola limpiada.")

            elif cmd == "force":
                kw = args["f"]
                if not kw or kw.lower() in ["off", "nada", "quitar", "desactivar"]:
                    player.config["forced_keyword"] = None
                    kw_status = "desactivado"
                else:
                    player.config["forced_keyword"] = kw
                    kw_status = f"activado: {kw}"
                save_discord_config(player.config)
                await ctx.send(f"🎙️ **Forzar (Discord)**: {kw_status}")
                if player.config["forced_keyword"]:
                    await player.play_query(kw, preempt=True, user_id=ctx.author.id)

            elif cmd == "radio":
                player.radio_mode = args["enabled"]
                await ctx.send(f"🎙️ **Radio (Discord)**: {'ON' if player.radio_mode else 'OFF'}")
                if player.radio_mode and not player.vc.is_playing() and not player.is_playing:
                     await player.play_next()

            elif cmd == "filtros":
                player.config["filters_enabled"] = args["enabled"]
                save_discord_config(player.config)
                await ctx.send(f"🎙️ **Filtros (Discord)**: {'ON' if args['enabled'] else 'OFF'}")

            elif cmd == "info":
                # Calcular tiempo transcurrido
                elapsed = 0
                if player.vc and (player.vc.is_playing() or player.vc.is_paused()):
                    if player.vc.is_playing():
                        elapsed = (time.time() - player.playback_start_time) + player.pause_offset
                    else:
                        elapsed = player.pause_offset
                
                # Función para formatear segundos a MM:SS
                def fmt(s):
                    if s is None or s < 0: return "00:00"
                    s = int(s)
                    return f"{s//60:02d}:{s%60:02d}"

                # Información de la canción actual
                title = "Nada"
                total_time = "00:00"
                if player._current_info:
                    title = player._current_info.get('title', 'Desconocido')
                    total_time = fmt(player._current_info.get('duration'))

                # Próxima canción
                next_track = "Nada en cola"
                if player.queue:
                    next_item = player.queue[0]
                    next_info = next_item[0] if isinstance(next_item, tuple) else next_item
                    next_track = next_info.get('title', 'Desconocido')
                elif player.radio_mode:
                    next_track = "📻 Automático (Radio)"

                # Estados
                r = "ON" if player.radio_mode else "OFF"
                f = "ON" if player.config.get("filters_enabled", True) else "OFF"
                fk = player.config.get("forced_keyword") or "OFF"
                
                status_emoji = "🎵" if player.vc.is_playing() else "⏸️"
                
                msg = (
                    f"***INFO***\n"
                    f"---------\n"
                    f"{status_emoji} **Nombre**: {title}\n"
                    f"⏳ **Tiempo**: `{fmt(elapsed)} / {total_time}`\n"
                    f"📻 Radio: {r} | 🛡️ Filtros: {f} | 🎯 Forzar: {fk}\n"
                    f"⏭️ **Siguiente**: {next_track}"
                )
                await ctx.send(msg)

            elif cmd == "help":
                 msg_voz = (
                     "***COMANDOS DE VOZ***\n"
                     "=====================\n\n"
                     "🎵 **CONTROLES DE REPRODUCCIÓN**\n"
                     "```\n"
                     "pon/play/reproduce [canción]  - Busca y reproduce una canción\n"
                     "pon playlist de [artista]     - Carga una playlist completa\n"
                     "pausa/pausar                  - Pausa la reproducción\n"
                     "continua/sigue/reanudar       - Reanuda la música\n"
                     "siguiente/pasa                - Salta a la siguiente canción\n"
                     "repetir/repite                - Repite la canción actual\n"
                     "parar/detener/stop            - Detiene todo y limpia la cola\n"
                     "```\n\n"
                     "🔊 **CONTROL DE SONIDO**\n"
                     "```\n"
                     "volumen [0-100][%]                  - Ajusta el volumen al porcentaje\n"
                     "sube/baja el volumen al [N][%]      - Ajusta el volumen a porcentaje específico\n"
                     "sube/baja el volumen                - Sube o baja el volumen en pasos\n"
                     "silencio/calla/cállate/mutea[te]    - Silencia el audio\n"
                     "habla/desmutéa[te]                  - Restaura el volumen\n"
                     "```\n\n"
                     "📋 **GESTIÓN DE COLA**\n"
                     "```\n"
                     "cola/lista/queue                    - Muestra las canciones en cola\n"
                     "quita [número]                      - Elimina una canción de la cola\n"
                     "vacía/limpia/quita/borra la cola    - Vacía toda la cola\n"
                     "```\n\n"
                     "⭐ **TOPS PERSONALES**\n"
                     "```\n"
                     "toplist                             - Muestra tu top de canciones\n"
                     "toplist global / globaltop          - Muestra el top global del servidor\n"
                     "topremove [número]                  - Elimina una canción de tu top\n"
                     "```\n\n"
                     "⚙️ **CONFIGURACIÓN**\n"
                     "```\n"
                     "radio on/off                        - Activa/desactiva modo radio\n"
                     "filtros on/off                      - Activa/desactiva filtros\n"
                     "forzar [palabra]                    - Fuerza búsquedas con palabra clave\n"
                     "forzar on/off                       - Activa/desactiva el forzado\n"
                     "```\n\n"
                     "ℹ️ **INFORMACIÓN**\n"
                     "```\n"
                     "info                                - Muestra estado y progreso\n"
                     "ayuda/help                          - Muestra este mensaje\n"
                     "salir                               - Desconecta el bot del canal\n"
                     "```"
                 )
                 
                 msg_texto = (
                     "***COMANDOS DE TEXTO***\n"
                     "=======================\n\n"
                     "💬 **COMANDOS CON PREFIJO !**\n"
                     "```\n"
                     "!join                               - Conecta el bot al canal de voz\n"
                     "!play [canción]                     - Reproduce una canción\n"
                     "!stop                               - Detiene la reproducción\n"
                     "!skip                               - Salta a la siguiente canción\n"
                     "!queue / !cola                      - Muestra la cola actual\n"
                     "!top / !toplist                     - Muestra tu top personal\n"
                     "!top @usuario                       - Muestra el top de otro usuario\n"
                     "!top global                         - Muestra el top global\n"
                     "!topremove [número]                 - Elimina una canción de tu top\n"
                     "!volume [0-100]                     - Ajusta el volumen\n"
                     "!info                               - Muestra información de reproducción\n"
                     "!listen                             - Activa/desactiva escucha de voz\n"
                     "!leave                              - Desconecta el bot\n"
                     "```\n\n"
                     "**Nota:** Los comandos de voz requieren usar la palabra clave (por defecto 'rafa') antes del comando.\n"
                     "Ejemplo: `rafa pon despacito` o usa `!play despacito`"
                 )
                 
                 await ctx.send(msg_voz)
                 await ctx.send(msg_texto)

            elif cmd == "exit":
                if player.vc:
                    await player.vc.disconnect()
                players.pop(ctx.guild.id, None)
                await ctx.send("👋 Adiós.")

            elif cmd == "voice":
                # Mapping 'voice' command (activar voz / desactivar voz / silencio) to listening toggle
                if not args["enabled"]:
                    await stop_listening(ctx)
                    player.config["listen_enabled"] = False
                    save_discord_config(player.config)
                    await ctx.send("🔇 **Escucha desactivada** (por comando de voz).")
                else:
                    # 'Activar voz' - if already listening, logic in start_listening handles it
                    hotwords = player.config.get("hotwords", ["rafa"])
                    success = await start_listening(ctx, hotwords)
                    if success: 
                        player.config["listen_enabled"] = True
                        save_discord_config(player.config)
                        await ctx.send(f"👂 **Escucha activada**. Hotwords: `{hotwords}`")
            
            elif cmd == "toplist":
                # Show user's top or global top
                data = load_top_data()
                is_global = args.get("is_global", False)
                user_id = args.get("user_id")
                
                if is_global:
                    # Show global top
                    if data["global_top"]:
                        msg = "🌍 **Top Global del Servidor (Máximo 20):**\n"
                        for i, song in enumerate(data["global_top"], 1):
                            msg += f"{i}. {song['title']}\n"
                        await ctx.send(msg)
                    else:
                        await ctx.send("❌ El top global está vacío. Reproduce algunas canciones primero.")
                elif user_id:
                    # Show specific user's top
                    user_id_str = str(user_id)
                    if user_id_str in data["user_tops"] and data["user_tops"][user_id_str]:
                        msg = f"🎵 **Top de <@{user_id}> (Máximo 5):**\n"
                        for i, song in enumerate(data["user_tops"][user_id_str], 1):
                            msg += f"{i}. {song['title']}\n"
                        await ctx.send(msg)
                    else:
                        await ctx.send(f"❌ <@{user_id}> no tiene canciones en su top.")
                else:
                    # Show current user's top
                    user_id_str = str(ctx.author.id)
                    if user_id_str in data["user_tops"] and data["user_tops"][user_id_str]:
                        msg = "🎵 **Tu Top (Máximo 5):**\n"
                        for i, song in enumerate(data["user_tops"][user_id_str], 1):
                            msg += f"{i}. {song['title']}\n"
                        await ctx.send(msg)
                    else:
                        await ctx.send("❌ No tienes canciones en tu top. Reproduce algunas canciones primero.")
            
            elif cmd == "topremove":
                # Remove song from user's top
                idx = args["n"] - 1  # Convert to 0-based
                success, message = remove_from_user_top(ctx.author.id, idx)
                if success:
                    await ctx.send(f"✅ {message}")
                else:
                    await ctx.send(f"❌ {message}")
            
            return True

        except Exception as e:
            logging.error(f"❌ Error ejecutando comando de voz '{cmd}': {e}")
            await ctx.send(f"❌ Error al ejecutar comando: {e}")
            return False
            
    except Exception as e:
        logging.error(f"❌ Error CRÍTICO en handle_voice_command: {e}")
        return False


async def start_listening(ctx, hotwords, prefixes=None):
    if not ctx.voice_client or not isinstance(ctx.voice_client, voice_recv.VoiceRecvClient):
        logging.error("❌ start_listening: VoiceClient no compatible o desconectado.")
        return False

    player = players.get(ctx.guild.id)
    if not player:
        logging.error("❌ start_listening: No hay player instanciado para este servidor.")
        return False

    # Si ya está en recording_tasks, verificamos si realmente está escuchando
    if ctx.guild.id in recording_tasks: 
        sink = recording_tasks[ctx.guild.id]
        if ctx.voice_client.is_listening():
            logging.info("👂 start_listening: Ya se está escuchando activamente.")
            return True
        else:
            logging.info("👂 [Fix] El sink existía pero no se estaba escuchando. Re-enganchando...")
            try:
                ctx.voice_client.listen(sink)
                return True
            except Exception as e:
                logging.error(f"❌ Error al re-enganchar: {e}")
                # Si falla, procedemos a crear uno nuevo abajo (limpiando el viejo)
                sink.cleanup()
                recording_tasks.pop(ctx.guild.id)

    try:
        sink = VoiceRecognitionSink(hotwords, prefixes, ctx, player)
        sink.process_audio.start()
        ctx.voice_client.listen(sink)
        sink.is_currently_listening = True
        recording_tasks[ctx.guild.id] = sink
        logging.info(f"👂 Escucha iniciada. El bot está listo para escucharte.")
        logging.info(f"ℹ️ Hotwords: {hotwords}, Prefijos: {prefixes}")
        return True
    except Exception as e:
        logging.error(f"❌ Error al iniciar escucha: {e}")
        return False

async def stop_listening(ctx):
    sink = recording_tasks.pop(ctx.guild.id, None)
    if sink:
        sink.cleanup()
        sink.is_currently_listening = False
        if ctx.voice_client: ctx.voice_client.stop_listening()
        logging.info("🔇 El bot ha dejado de escuchar.")
        return True
    return False

@bot.command()
async def listen(ctx):
    if not ctx.voice_client: await join(ctx)
    if not ctx.voice_client: return

    player = players.get(ctx.guild.id)
    d_config = load_discord_config()
    current_state = d_config.get("listen_enabled", False)
    new_state = not current_state # Toggle
    
    d_config["listen_enabled"] = new_state
    save_discord_config(d_config)
    
    hotwords = d_config.get("hotwords", ["rafa"])
    prefixes = d_config.get("activation_prefixes", ["oye"])

    if new_state:
        # Activar
        success = await start_listening(ctx, hotwords, prefixes)
        if success:
            if player: player.config["listen_enabled"] = True
            await ctx.send(f"👂 **Escucha ACTIVADA**. Hotwords: `{hotwords}`, Prefijos: `{prefixes}`")
        else: await ctx.send("❌ Error al activar escucha.")
    else:
        # Desactivar
        await stop_listening(ctx)
        if player: player.config["listen_enabled"] = False
        await ctx.send("🔇 **Escucha DESACTIVADA** (Toggle).")

# Alias para desactivar explícitamente si se desea, aunque listen es toggle ahora.
@bot.command()
async def silence(ctx):
    d_config = load_discord_config()
    d_config["listen_enabled"] = False
    save_discord_config(d_config)
    player = players.get(ctx.guild.id)
    if player: player.config["listen_enabled"] = False
    await stop_listening(ctx)
    await ctx.send("🔇 **Escucha desactivada**.")

@bot.command()
async def status(ctx):
    """Comando de diagnóstico para verificar estado del bot de voz"""
    player = players.get(ctx.guild.id)
    if not player:
        return await ctx.send("❌ Bot no conectado al canal de voz")
    
    # Verificar estado del voice client
    vc_connected = player.vc and player.vc.is_connected()
    vc_listening = player.vc and player.vc.is_listening()
    
    # Verificar sink
    sink = recording_tasks.get(ctx.guild.id)
    sink_active = sink is not None
    packets_received = sink.packet_count if sink else 0
    loop_running = sink.process_audio.is_running() if sink and hasattr(sink, 'process_audio') else False
    
    # Config
    listen_enabled = player.config.get("listen_enabled", False)
    hotwords = player.config.get("hotwords", [])
    
    msg = (
        "🔍 **DIAGNÓSTICO DE VOZ**\n"
        "```\n"
        f"Voice Client Conectado: {'✅' if vc_connected else '❌'}\n"
        f"Voice Client Escuchando: {'✅' if vc_listening else '❌'}\n"
        f"Sink Activo: {'✅' if sink_active else '❌'}\n"
        f"Loop de Procesamiento: {'✅' if loop_running else '❌'}\n"
        f"Paquetes Recibidos: {packets_received}\n"
        f"Listen Enabled (config): {'✅' if listen_enabled else '❌'}\n"
        f"Hotwords: {hotwords}\n"
        "```\n"
    )
    
    if not vc_listening and listen_enabled:
        msg += "\n⚠️ **PROBLEMA DETECTADO**: Listen está habilitado pero el bot no está escuchando.\n"
        msg += "Usa `!listen` para reactivar la escucha."
    elif packets_received == 0 and vc_listening:
        msg += "\n⚠️ **ADVERTENCIA**: Bot escuchando pero no recibe paquetes de audio.\n"
        msg += "Verifica que estés hablando en el canal y que tu micrófono funcione."
    elif packets_received > 0:
        msg += f"\n✅ Bot recibiendo audio correctamente ({packets_received} paquetes)"
    
    await ctx.send(msg)

@bot.command()
async def volume(ctx, vol: int):
    # vol 0-100
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    
    player.set_volume(vol)
    player.config["volume"] = player._volume
    save_discord_config(player.config)
    await ctx.send(f"🔊 Volumen ajustado a **{vol}%**")

@bot.command(name="queue", aliases=["q", "cola", "lista"])
async def show_queue(ctx):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    if not player.queue: return await ctx.send("📋 La cola está vacía.")
    
    msg = "📋 **Cola actual**:\n"
    for i, item in enumerate(player.queue[:10], 1):
        info = item[0] if isinstance(item, tuple) else item
        msg += f"**{i}.** {info.get('title', '???')}\n"
    if len(player.queue) > 10:
        msg += f"\n... y {len(player.queue)-10} canciones más."
    await ctx.send(msg)

@bot.command(name="remove", aliases=["quitar", "borrar", "delete"])
async def remove_item(ctx, index: int):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    
    idx = index - 1
    if 0 <= idx < len(player.queue):
        item = player.queue.pop(idx)
        info = item[0] if isinstance(item, tuple) else item
        await ctx.send(f"🗑️ Eliminado de la cola: **{info.get('title')}**")
    else:
        await ctx.send(f"❌ Índice {index} no válido. Usa `!queue` para ver la lista.")

@bot.command(name="clear", aliases=["limpiar", "vaciar"])
async def clear_queue(ctx):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    player.queue.clear()
    await ctx.send("🧹 Cola limpiada correctamente.")

@bot.command()
async def force(ctx, *, keyword: str = None):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    
    if not keyword or keyword.lower() in ["off", "nada", "quitar", "desactivar"]:
        player.config["forced_keyword"] = None
        ans = "desactivado"
    else:
        player.config["forced_keyword"] = keyword
        ans = f"activado: {keyword}"
    
    save_discord_config(player.config)
    await ctx.send(f"🎯 Forzar **{ans}**")
    
    if player.config["forced_keyword"]:
        await player.play_query(keyword, preempt=True)

@bot.command()
async def radio(ctx, state: str):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    
    enabled = state.lower() in ["on", "activar", "si", "true"]
    player.radio_mode = enabled
    await ctx.send(f"📻 Radio **{'ON' if enabled else 'OFF'}**")
    if enabled and not player.vc.is_playing() and not player.is_playing:
         await player.play_next()

@bot.command()
async def info(ctx):
    player = players.get(ctx.guild.id)
    if not player: return await ctx.send("❌ No estoy conectado.")
    
    # Usar la misma lógica que los comandos de voz
    await handle_voice_command(ctx, player, "info")


@bot.command(name='top', aliases=['toplist'])
async def top_command(ctx, *, args=None):
    """Muestra el top de canciones (personal, de otro usuario, o global)"""
    player = players.get(ctx.guild.id)
    if not player:
        return await ctx.send("❌ El bot no está conectado. Usa `!join` primero.")
    
    # Parse arguments to determine what to show
    if args:
        args_lower = args.lower().strip()
        if 'global' in args_lower:
            await handle_voice_command(ctx, player, "toplist global")
        elif ctx.message.mentions:
            # User mentioned someone
            mentioned_user = ctx.message.mentions[0]
            await handle_voice_command(ctx, player, f"toplist <@{mentioned_user.id}>")
        else:
            # Try to interpret as user mention or show own
            await handle_voice_command(ctx, player, "toplist")
    else:
        # Show user's own top
        await handle_voice_command(ctx, player, "toplist")

@bot.command(name='topremove')
async def topremove_command(ctx, number: int):
    """Elimina una canción de tu top personal"""
    player = players.get(ctx.guild.id)
    if not player:
        return await ctx.send("❌ El bot no está conectado. Usa `!join` primero.")
    
    await handle_voice_command(ctx, player, f"topremove {number}")

@bot.command(name='help')
async def help_command(ctx):
    """Shows comprehensive help for all bot commands"""
    player = players.get(ctx.guild.id)
    if not player:
        # Create a minimal player context just for help
        temp_config = load_discord_config()
        temp_player = type('obj', (object,), {
            'config': temp_config,
            'radio_mode': False,
            'vc': None,
            '_current_info': None,
            'queue': []
        })()
        await handle_voice_command(ctx, temp_player, "help")
    else:
        await handle_voice_command(ctx, player, "help")

@bot.command(name='ayuda')
async def ayuda_command(ctx):
    """Muestra ayuda completa de todos los comandos del bot (alias español)"""
    await help_command(ctx)


def init_opus():
    """Busca e intenta cargar libopus desde site-packages."""
    if discord.opus.is_loaded(): return
    
    try:
        import site
        prefixes = site.getsitepackages()
        for p in prefixes:
            # Ruta típica para py-cord en Windows
            opus_path = os.path.join(p, "discord", "bin", "libopus-0.x64.dll")
            if os.path.exists(opus_path):
                discord.opus.load_opus(opus_path)
                logging.info(f"✅ Opus cargado desde: {opus_path}")
                return
            # Fallback a x86
            opus_path = os.path.join(p, "discord", "bin", "libopus-0.x86.dll")
            if os.path.exists(opus_path):
                discord.opus.load_opus(opus_path)
                logging.info(f"✅ Opus cargado (x86) desde: {opus_path}")
                return
    except Exception as e:
        logging.warning(f"No se pudo cargar Opus automáticamente: {e}")

# --- Monkey Patch for Opus Decoding ---
# Fixes 'discord.opus.OpusError: corrupted stream' crashing the voice_recv thread
def patch_opus_decoder():
    import discord.opus
    
    original_decode = discord.opus.Decoder.decode

    def safe_decode(self, data, fec=False):
        try:
            return original_decode(self, data, fec=fec)
        except discord.opus.OpusError as e:
            if "corrupted stream" in str(e).lower():
                logging.debug("⚠️ Paquete Opus corrupto ignorado (devuelta silencio).")
                # Return 20ms of silence for stereo 48kHz (frame_size=960) -> 960 * 2 * 2 = 3840 bytes
                return b'\x00' * 3840 
            raise e

    discord.opus.Decoder.decode = safe_decode
    logging.info("🔧 Monkey-patch aplicado a discord.opus.Decoder.decode para resistencia a errores.")

if __name__ == "__main__":
    check_single_instance()
    # En discord.py 2.0+ no suele ser necesario el loop policy hack, pero si falla lo activamos.
    # Por ahora lo quitamos para probar limpieza.
    
    cleanup_temp_files(prefix=DISCORD_PREFIX)
    init_opus()
    patch_opus_decoder() # Apply patch after init
    d_config = load_discord_config()
    token = d_config.get("discord_token")
    if not token or token == "TU_TOKEN_AQUI":
        print("❌ Falta token de Discord.")
    else:
        bot.run(token)
