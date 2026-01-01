# -*- coding: utf-8 -*-
"""
vtm.py — Reproducir música de YouTube mediante comandos de voz o texto (ES)
Decodificado de vtm_core.py.
"""

VTM_VERSION = "0.9.0"
UPDATE_URL = "https://raw.githubusercontent.com/Cicker21/VTM/refs/heads/main/vtm.py"

import argparse
import logging
import os
import webbrowser
import time
import difflib
import threading
import speech_recognition as sr
import json
import uuid
import re
import atexit
import urllib.request

from yt_dlp import YoutubeDL
from ffpyplayer.player import MediaPlayer

# --- Configuraciones y Rutas ---
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "config.json")
FAV_FILE = os.path.join(_SCRIPT_DIR, "favorites.json")
PLAYLIST_FILE = os.path.join(_SCRIPT_DIR, "playlists.json")
TEMP_AUDIO_PREFIX = "vtm_local_"

AYUDA_MSG = (
    "\n📋 COMANDOS (VTM)\n\n"
    "🎵 REPRODUCCIÓN\n"
    "- p / pon [q]        Reproducir canción o búsqueda\n"
    "- p                  Pausa / Reanudar (Toggle)\n"
    "- s / n / siguiente  Siguiente canción\n"
    "- stop / detener     Para la música\n"
    "- replay / otra vez  Reinicia el tema actual\n"
    "- ap / historial     Canciones que ya han sonado\n"
    "- r / shuffle / aleat Mezclar la cola actual\n"
    "- add / a [q]        Añadir a la cola sin interrumpir\n\n"
    
    "🔊 AUDIO Y CONTROL\n"
    "- + / - / v [n]      Subir/Bajar volumen o fijar [0-200]\n"
    "- m / silencio       Silenciar (Toggle)\n"
    "- micro [on/off]     Activar/Desactivar micrófono\n"
    "- micros / miclist   Listar micrófonos disponibles\n"
    "- micro [n]          Cambiar a micrófono índice [n]\n\n"
    
    "📂 PLAYLISTS\n"
    "- ps / playlists     Ver todas tus listas importadas\n"
    "- import [url]       Importar lista de YouTube\n"
    "- pp [nombre]        Reproducir una de tus listas\n"
    "- pr [nombre]        Eliminar una playlist\n"
    "- pc / pc2 [q]       Verificar links (pc2 = modo profundo)\n\n"
    
    "⭐️ FAVORITOS\n"
    "- fav / me gusta     Guardar actual en favoritos\n"
    "- favlast            Guardar la anterior en favoritos\n"
    "- fp / playfav       Reproducir tus favoritos\n"
    "- rf / favrandom     Modo aleatorio de favoritos\n"
    "- favlist            Listar todos tus favoritos\n"
    "- favcheck           Verificar disponibilidad de favoritos\n\n"
    
    "⚙️ AJUSTES Y SISTEMA\n"
    "- info / estado      ¿Qué está sonando?\n"
    "- radio [on/off]     Modo radio al vaciarse la cola\n"
    "- con/sin filtros    Activar/Quitar filtros de YouTube\n"
    "- forzar [palabra]   Filtrar radio por una palabra clave\n"
    "- modo [directo/nav] Cambia motor de descarga\n"
    "- h / ayuda / help   Mostrar esta lista\n"
    "- salir / terminar   Cerrar la aplicación"
)

# --- Logger de yt-dlp ---
class YtdlLogger:
    def debug(self, msg): pass
    def warning(self, msg):
        ignore_keywords = ["JavaScript runtime", "web_safari", "PO Token", "web client", "ios client"]
        if not any(k in msg for k in ignore_keywords):
            logging.warning(f"[yt-dlp] {msg}")
    def error(self, msg):
        logging.error(f"[yt-dlp ERROR] {msg}")

# --- Gestión de Configuración ---
def load_config():
    defaults = {
        "blacklisted_keywords": ["live", "concierto", "vivo", "remix", "bazar", "album", "playlist", "mix", "tutorial", "compilation"],
        "max_duration_seconds": 600,
        "hotwords": ["rafa"],
        "filters_enabled": True,
        "volume": 0.02,
        "microphone_index": None,
        "forced_keyword": None,
        "shorts_keywords": ["#shorts", "shorts", "reels"],
        "max_shorts_duration": 65,
        "listen_enabled": True
    }
    if not os.path.exists(CONFIG_FILE):
        return defaults
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in defaults.items():
                if k not in data: data[k] = v
            
            # Normalize hotwords
            hw_list = data.get("hotwords", [])
            hw_single = data.get("hotword") # Legacy
            
            if isinstance(hw_list, list) and hw_list:
                data["hotwords"] = [h.lower() for h in hw_list if isinstance(h, str)]
            elif isinstance(hw_single, str):
                data["hotwords"] = [hw_single.lower()]
            else:
                data["hotwords"] = ["rafa"]

            data["forced_keyword"] = None # Reset forced keyword
            return data
    except Exception as e:
        logging.error(f"Error cargando config: {e}")
        return defaults

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        logging.error(f"Error guardando config: {e}")

def load_favorites():
    if not os.path.exists(FAV_FILE): return []
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except: return []

def save_favorites(favs):
    try:
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favs, f, indent=4)
    except Exception as e:
        logging.error(f"Error guardando favoritos: {e}")

def load_playlists():
    if not os.path.exists(PLAYLIST_FILE): return {}
    try:
        with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Migración: Si es una lista, la metemos en 'Mi Lista'
            if isinstance(data, list):
                if not data: return {}
                return {"migrated": {"title": "Mi Lista", "songs": data}}
            return data
    except: return {}

def save_playlists(plist):
    try:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(plist, f, indent=4)
    except Exception as e:
        logging.error(f"Error guardando playlists: {e}")


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

    # Filtro de duración
    max_dur = config.get("max_duration_seconds", 600)
    if duration > max_dur:
        logging.info(f"🚫 Saltado (Demasiado largo: {duration}s > {max_dur}s): {title}")
        return False

    # Filtro de Shorts
    shorts_kws = config.get("shorts_keywords", ["#shorts", "shorts", "reels"])
    max_shorts_dur = config.get("max_shorts_duration", 65)
    is_short = any(k.lower() in title for k in shorts_kws)
    if is_short and duration <= max_shorts_dur:
        logging.info(f"🚫 Saltado (YouTube Short Detectado): {title} ({duration}s)")
        return False

    # Filtro de tipo de contenido (Evitar canales/playlists)
    e_type = info.get("_type", "video")
    if e_type not in ["video", "url", "url_transparent"]:
        logging.info(f"🚫 Saltado (No es un video/url: {e_type}): {title}")
        return False

    return True

def get_search_info(query: str, index: int = 0):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'logger': YtdlLogger(),
        'extract_flat': True,
        'lazy_playlist': True,
        'playlist_items': '1-10',
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
            if not res: 
                logging.warning(f"DEBUG: YoutubeDL no devolvió resultados para: {search_query}")
                return None
            
            entries = res.get("entries", [])
            logging.info(f"DEBUG: Búsqueda '{query}' devolvió {len(entries)} entradas.")
            if not entries and "title" in res:
                # Si es un vídeo directo (no búsqueda), le inyectamos los campos mínimos
                if "url" not in res and "webpage_url" in res:
                    res["url"] = res["webpage_url"]
                return res
            
            if index < len(entries):
                entry = entries[index]
                e_title = entry.get("title", "Sin título")
                e_url = entry.get("url") or entry.get("webpage_url")
                e_type = entry.get("_type", "video") # Por defecto asumimos video si no viene

                logging.info(f"DEBUG: Evaluando entrada {index}: '{e_title}' (Tipo: {e_type})")

                if not entry.get("duration") and e_url and e_type in ["url", "url_transparent"]:
                    logging.info(f"DEBUG: No se detectó duración, extrayendo info extendida de {e_url}...")
                    with YoutubeDL(ydl_opts) as ydl2:
                        full_info = ydl2.extract_info(e_url, download=False)
                        if full_info and full_info.get("_type") == "playlist":
                            # Inyectamos el tipo para que is_content_allowed lo sepa
                            full_info["_type"] = "playlist"
                        return full_info
                return entry
    except Exception as e:
        logging.error(f"Error búsqueda: {e}")
    return None

def download_media(url, prefix=TEMP_AUDIO_PREFIX):
    if not url: return None
    filename = f"{prefix}{uuid.uuid4()}.m4a"
    filepath = os.path.join(_SCRIPT_DIR, filename)

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
            exit_code = ydl.download([url])
            if exit_code == 0 and os.path.exists(filepath):
                return filepath
            else:
                logging.error(f"❌ Error en descarga: exit_code={exit_code}, file_exists={os.path.exists(filepath)}")
    except Exception as e:
        logging.error(f"Excepción en download_media: {e}")
    return None

def get_recommendations(video_id: str):
    # Usamos el parámetro list=RD... para forzar a YouTube a darnos la "radio" del vídeo
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    ydl_opts = {
        'format': 'bestaudio/best', 
        'quiet': True, 
        'no_warnings': True, 
        'extract_flat': True, 
        'lazy_playlist': True,
        'playlist_items': '1-15'
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
             info = ydl.extract_info(url, download=False)
             # En un mix de YouTube, las recomendaciones vienen en 'entries'
             entries = info.get("entries", [])
             return [{"id": e["id"], "title": e["title"], "duration": e.get("duration")} for e in entries if e]
    except: return []

# --- Utilidades de Hardware ---
def get_input_devices():
    indices = []
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        numdevices = p.get_host_api_info_by_index(0).get('deviceCount', 0)
        for i in range(numdevices):
            if p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels', 0) > 0:
                indices.append(i)
        p.terminate()
    except:
        try: indices = list(range(len(sr.Microphone.list_microphone_names())))
        except: pass
    return indices

def resolve_mic_index(preferred):
    available = get_input_devices()
    if not available: return None
    if isinstance(preferred, int): preferred = [preferred]
    elif not isinstance(preferred, list): preferred = []
    for idx in preferred:
        if idx in available: return idx
    return max(available)

def list_microphones():
    print("🎤 Micrófonos disponibles (Entradas):")
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        numdevices = p.get_host_api_info_by_index(0).get('deviceCount', 0)
        for i in range(numdevices):
            info = p.get_device_info_by_host_api_device_index(0, i)
            if info.get('maxInputChannels', 0) > 0:
                print(f"  [{i}] {info.get('name')}")
        p.terminate()
    except:
        for index, name in enumerate(sr.Microphone.list_microphone_names()):
             print(f"  [{index}] {name}")

def cleanup_temp_files(prefix=TEMP_AUDIO_PREFIX):
    for f in os.listdir(_SCRIPT_DIR):
        if f.startswith(prefix) and (f.endswith(".mp4") or f.endswith(".m4a")):
            try: os.remove(os.path.join(_SCRIPT_DIR, f))
            except: pass

# --- Parser de Comandos ---
class CommandParser:
    RE_PRODUCIR = re.compile(r"(reproduce|reproducir|pon|poner)(me|te)?\s+(m[uú]sica\s+de\s+|la\s+canci[oó]n\s+)?(?P<q>.+)$", re.I)
    RE_FAV = re.compile(r"^(fav(orito)?|me gusta)$", re.I)
    RE_FAVLAST = re.compile(r"^(favlast|prefav|favprev|fav anterior|la anterior me gusta)$", re.I)
    RE_FAVLIST = re.compile(r"^(favlist|lista de favoritos|mis favoritos)$", re.I)
    RE_PLAYFAV = re.compile(r"^(playfav|favplay|reproduce favoritos|pon favoritos|favoritos|fp|pf)$", re.I)
    RE_FAVCHECK = re.compile(r"^(favcheck|checkfavs|verificar favoritos)$", re.I)
    RE_FAV_RANDOM = re.compile(r"^(rf|favrandom)$", re.I)
    RE_IMPORT = re.compile(r"^(import|importar)\s+(?P<url>https?://[^\s]+)$", re.I)
    RE_PLAYLIST = re.compile(r"^(pp|playlist|lista)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_REMOVE = re.compile(r"^(pr|ppremove|playlistremove)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLISTS = re.compile(r"^(ps|playlists)$", re.I)
    RE_PLAYLIST_CHECK = re.compile(r"^(pc|playlistcheck|playlist check|checkplaylist)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_CHECK_DEEP = re.compile(r"^(pc2|deepcheck)(\s+(?P<q>.+))?$", re.I)


    RE_HISTORY = re.compile(r"^(ap|historial|history|ya sonaron)$", re.I)
    RE_SHUFFLE = re.compile(r"^(r|shuffle|random|aleatorio|mezcla(r)?)$", re.I)
    RE_ADD = re.compile(r"^(add|a|cola|añadir)\s+(?P<q>.+)$", re.I)
    RE_PAUSA = re.compile(r"^(pausa|pausar)$", re.I)
    RE_CONTINUAR = re.compile(r"^(continuar|reanudar)$", re.I)
    RE_TOGGLE_P = re.compile(r"^p$", re.I)
    RE_PLAY_P = re.compile(r"^p\s+(?P<q>.+)$", re.I)
    RE_DETENER = re.compile(r"(detener|parar|stop)", re.I)
    RE_SALIR = re.compile(r"(salir|terminar)", re.I)
    RE_SIGUIENTE = re.compile(r"^(siguiente|pr[oó]ximo|pasa|s|n)$", re.I)
    RE_VOL = re.compile(r"(poner\s+|fijar\s+)?volumen\s*(al\s+|a\s+|en\s+)?(?P<n>\d{1,3})\s*%?$", re.I)
    RE_VOL_UP = re.compile(r"^(\+|sube|subir|m[áa]s)$", re.I)
    RE_VOL_DOWN = re.compile(r"^(-|baja|bajar|menos)$", re.I)
    RE_VOL_REL = re.compile(r"(?P<op>sube|subir|baja|bajar|m[áa]s|menos)\s+(el\s+|la\s+)?(volumen|m[uú]sica|audio|alto|bajo)(?P<amount>\s+un\s+poco)?", re.I)
    RE_MUTE = re.compile(r"^(silencio|calla(te)?|cállate|mute|shh|sh|m)$", re.I)
    RE_UNMUTE = re.compile(r"(habla|unmute|devuelve (el )?sonido|sonido|audio on)", re.I)
    RE_REPLAY = re.compile(r"(repite|repetir|otra vez|ponla de nuevo|reinicia(r)?|bise|reus)", re.I)
    RE_MODO = re.compile(r"modo\s+(?P<m>(navegador|directo))", re.I)
    RE_RADIO = re.compile(r"(encender|activar|apagar|desactivar|radio|auto-?dj|modo radio)\s*(radio|auto-?dj|modo radio)?\s*(?P<op>on|off)?", re.I)
    RE_FILTROS = re.compile(r"(activar|desactivar|quitar|poner|sin|con|apaga(r?)|enciende|encender)?\s*(los\s+)?filtros?\s*(?P<op>on|off)?", re.I)
    RE_INFO = re.compile(r"(info|informaci[oó]n|qu[eé]\s+suena|estado)", re.I)
    RE_FORCE = re.compile(r"forzar\s+(?P<f>.+)", re.I)
    RE_AYUDA = re.compile(r"^(ayuda|help|opciones|comandos|h)$", re.I)
    RE_MICRO = re.compile(r"micro(fono)?\s+(?P<n>\d+)", re.I)
    RE_LISTAR_MICROS = re.compile(r"(listar\s+)?(micros|microfonos|micrófonos|devices|dispositivos)", re.I)
    RE_LISTEN = re.compile(r"(activar|desactivar|apagar|encender|pon|quitar|con|sin)?\s*(el\s+)?(micro|micr[oó]fono|escuchar)\s*(?P<op>on|off)?", re.I)
    RE_FILTROS_OP = re.compile(r"(?P<kw>activar|desactivar|quitar|poner|sin|con|apaga(r?)|enciende|encender)", re.I)
    RE_VOL_SHORT = re.compile(r"^(v|vol)\s+(?P<n>\d{1,3})$", re.I)

    def parse(self, text: str):
        raw = text.strip()
        t = raw.lower()

        if self.RE_AYUDA.search(raw): return ("help", {})
        if self.RE_PLAYFAV.search(raw): return ("playfav", {}) # Prioridad sobre 'play'
        m = self.RE_PLAYLIST.search(raw)
        if m: return ("playlist", {"q": m.group("q")})

        m = self.RE_PLAYLIST_REMOVE.search(raw)
        if m: return ("playlist_remove", {"q": m.group("q")})

        if self.RE_PLAYLISTS.search(raw): return ("playlists", {})
        m = self.RE_PLAYLIST_CHECK.search(raw)
        if m: return ("playlistcheck", {"q": m.group("q")})

        m = self.RE_PLAYLIST_CHECK_DEEP.search(raw)
        if m: return ("playlistcheck_deep", {"q": m.group("q")})


        
        m = self.RE_IMPORT.search(raw)
        if m: return ("import", {"url": m.group("url")})
        
        if self.RE_HISTORY.search(raw): return ("history", {})
        if self.RE_SHUFFLE.search(raw): return ("shuffle", {})
        
        m = self.RE_ADD.search(raw)
        if m: return ("add", {"query": m.group("q")})
        
        if self.RE_LISTAR_MICROS.search(raw): return ("list_mics", {})
        if self.RE_PAUSA.search(raw): return ("pause", {})
        if self.RE_CONTINUAR.search(raw): return ("resume", {})
        if self.RE_TOGGLE_P.search(raw): return ("toggle", {})
        
        m = self.RE_PLAY_P.search(raw)
        if m: return ("play", {"query": m.group("q")})
        
        if self.RE_DETENER.search(raw): return ("stop", {})
        if self.RE_SALIR.search(raw): return ("exit", {})
        if self.RE_SIGUIENTE.search(raw): return ("next", {})
        if self.RE_INFO.search(raw): return ("info", {})
        if self.RE_MUTE.search(raw): return ("mute", {})
        if self.RE_UNMUTE.search(raw): return ("unmute", {})
        if self.RE_REPLAY.search(raw): return ("replay", {})
        
        if self.RE_FAV.search(raw): return ("fav", {})
        if self.RE_FAVLAST.search(raw): return ("favlast", {})
        if self.RE_FAVLIST.search(raw): return ("favlist", {})
        if self.RE_FAVCHECK.search(raw): return ("favcheck", {})
        
        m = self.RE_PRODUCIR.search(raw)
        if m: return ("play", {"query": m.group("q")})
        
        m = self.RE_VOL.search(raw)
        if m: return ("volume", {"n": int(m.group("n"))})
        
        m = self.RE_VOL_SHORT.search(raw)
        if m: return ("volume", {"n": int(m.group("n"))})
        
        if self.RE_VOL_UP.search(raw): return ("volume_rel", {"direction": "up"})
        if self.RE_VOL_DOWN.search(raw): return ("volume_rel", {"direction": "down"})

        m = self.RE_VOL_REL.search(raw)
        if m:
            op = m.group("op").lower()
            direction = "down" if any(x in op for x in ["baja", "menos"]) else "up"
            return ("volume_rel", {"direction": direction})

        m = self.RE_MODO.search(raw)
        if m: return ("mode", {"m": m.group("m").lower()})
        
        m = self.RE_RADIO.search(raw)
        if m:
            op = (m.group("op") or "").lower()
            enabled = True
            if op == "off": enabled = False
            elif any(x in t for x in ["off", "apagar", "desactivar"]) and not (op == "on"): enabled = False
            return ("radio", {"enabled": enabled})

        m = self.RE_FILTROS.search(raw)
        if m:
            op = (m.group("op") or "").lower()
            kw_match = self.RE_FILTROS_OP.search(raw)
            kw = kw_match.group(0).lower() if kw_match else ""
            
            if op == "on" or any(x in kw for x in ["activar", "poner", "con", "enciende", "encender"]):
                return ("filtros", {"enabled": True})
            if op == "off" or any(x in kw for x in ["desactivar", "quitar", "sin", "apaga"]):
                return ("filtros", {"enabled": False})
            
            return ("filtros", {"enabled": "toggle"})

        m = self.RE_FORCE.search(raw)
        if m: return ("force", {"f": m.group("f").strip()})

        m = self.RE_MICRO.search(raw)
        if m: return ("set_mic", {"n": int(m.group("n"))})

        m = self.RE_LISTEN.search(raw)
        if m:
            op = (m.group("op") or "").lower()
            enabled = True
            if op == "off": enabled = False
            elif any(x in t for x in ["desactivar", "apagar", "quitar", "sin", "off"]) and not (op == "on"): enabled = False
            return ("listen", {"enabled": enabled})

        return (None, {})

# Configuración de logging global
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def on_exit_hook():
    logging.info("🛑 [VTM EXIT HOOK] El proceso está terminando...")

atexit.register(on_exit_hook)

LOCAL_PREFIX = "vtm_local_"

# -----------------------------------------------------------
# Reproductor basado en FFPyPlayer
# -----------------------------------------------------------
class AudioPlayer:
    def __init__(self, radio_enabled: bool = True):
        self._player = None
        self._last_query = None
        self._last_index = 0
        self._paused = False
        self._current_title = None
        self._current_id = None
        self._current_info = None
        self._current_filepath = None
        self.history = []
        self._previous_info = None
        self.radio_mode = radio_enabled 
        self._manually_stopped = True
        self.config = load_config()
        self._volume = self.config.get("volume", 0.02)
        logging.info(f"🔊 Volumen inicial cargado: {int(self._volume * 1000)}%")
        self.forced_keyword = self.config.get("forced_keyword")
        
        # Modo Playlist de Favoritos
        self.fav_playlist_mode = False
        self.fav_index = 0
        
        # Modo Playlist Importada
        self.plist_mode = False
        self.plist_index = 0
        self.plist_id = None
        self.plist_title = None
        
        self._preloaded_data = None
        self._preloading = False
        self.queue = []
        self._lock = threading.RLock()


    def toggle_radio(self, enabled: bool):
        self.radio_mode = enabled
        logging.info(f"📻 Radio {'activada' if enabled else 'desactivada'}")

    def toggle_filters(self, enabled: bool | str):
        if enabled == "toggle":
            enabled = not self.config.get("filters_enabled", True)
        self.config["filters_enabled"] = enabled
        save_config(self.config)
        logging.info(f"🛡️ Filtros {'activados' if enabled else 'desactivados'}")

    def set_forced_filter(self, keyword: str | None):
        if not keyword or keyword.lower() in ["off", "nada", "quitar", "desactivar"]:
            self.forced_keyword = None
        else:
            self.forced_keyword = keyword
        self.config["forced_keyword"] = self.forced_keyword
        save_config(self.config)
        return self.forced_keyword

    def _start_playback(self, info, filepath):
        """Inicia el reproductor y elimina el archivo anterior."""
        with self._lock:
            old_path = self._current_filepath
            self.stop_locked()
            
            # Limpieza agresiva: borrar anterior
            if old_path and os.path.exists(old_path) and old_path != filepath:
                try: 
                    os.remove(old_path)
                except: pass
            
            # Delay para evitar colisión de hilos de SDL/ffpyplayer entre tracks
            time.sleep(0.5)

            try:
                # Inicializamos pausado y con volumen 0 para evitar COMPLETAMENTE el spike
                ff_opts = {'vn': True, 'volume': 0.0}
                logging.info(f"DEBUG: MediaPlayer init (paused=True) with: {filepath}")
                try:
                    # Usamos una variable local para evitar que otros hilos accedan a un objeto a medio inicializar
                    new_player = MediaPlayer(filepath, paused=True, ff_opts=ff_opts)
                    if not new_player:
                        logging.error("DEBUG: MediaPlayer returned None without Exception!")
                        return None
                    self._player = new_player
                except Exception as e:
                    logging.error(f"❌ Fallo crítico al crear MediaPlayer: {e}")
                    return None
                
                # Un breve respiro para que el objeto se asiente internamente
                time.sleep(0.2)
                
                # Seteamos el volumen objetivo mientras está pausado
                if self._player:
                    logging.info(f"DEBUG: Setting volume to {self._volume}")
                    try:
                        self._player.set_volume(self._volume)
                    except Exception as ev:
                        logging.error(f"DEBUG: Error setting initial volume: {ev}")
                    
                    time.sleep(0.1)
                    
                    # Quitamos la pausa
                    logging.info("DEBUG: Attempting to unpause player...")
                    try:
                        self._player.set_pause(False)
                        logging.info("DEBUG: Player unpaused successfully.")
                    except Exception as ep:
                        logging.error(f"DEBUG: Error unpausing player: {ep}")
                else:
                    logging.error("DEBUG: self._player is None after init!")
                
                # Forzamos el volumen un par de veces más por si acaso
                def force_volume():
                    for _ in range(8):
                        time.sleep(0.2)
                        with self._lock:
                            if self._player:
                                try:
                                    self._player.set_volume(self._volume)
                                except: pass
                
                threading.Thread(target=force_volume, daemon=True).start()
                
                self._previous_info = self._current_info
                self._current_filepath = filepath
                self._paused = False
                self._current_title = info["title"]
                self._current_id = info["id"]
                self._current_info = info
                self._current_duration = info.get("duration", 0)
                
                self.history.append(info["title"])
                if len(self.history) > 15:
                    self.history.pop(0) # Borra la más vieja
                
                self._manually_stopped = False
                time.sleep(0.4)
                logging.info(f"▶️ Reproduciendo: {info['title']}")
                
                self._preloaded_data = None
                self._preloading = False
                
                # Si no es un favorito manual, paramos el modo playlist
                if not info.get("_is_fav_playlist", False):
                    self.fav_playlist_mode = False
                
                # Si no es de la playlist importada, paramos el modo
                if not info.get("_is_plist", False):
                    self.plist_mode = False
                
                return info
            except Exception as e:
                import traceback
                logging.error(f"❌ Error playback: {e}")
                traceback.print_exc()
                return None


    def play_query(self, query: str, open_in_browser: bool, index: int = 0):
        # logging.info(f"DEBUG: play_query called with query='{query}', index={index}")
        self.fav_playlist_mode = False # Reset playlist
        self.plist_mode = False
        self._last_query = query
        self._last_index = index

        if open_in_browser:
            url = query if query.startswith("http") else f"https://www.youtube.com/results?search_query={query}"
            webbrowser.open(url)
            return

        # Buscamos un resultado que pase los filtros
        info = None
        for i in range(10):
            candidate = get_search_info(query, index=index + i)
            if not candidate: break
            
            c_title = candidate.get("title", "Sin título")
            
            # Si ya está sonando algo muy similar, lo saltamos para buscar el siguiente de Metrika, etc.
            if self._current_title:
                similar, _ = self._is_too_similar(self._current_title, c_title)
                if similar:
                    logging.info(f"⏳ Saltando (Ya está sonando): {c_title}")
                    continue

            if is_content_allowed(candidate, self.config):
                info = candidate
                break
            else:
                logging.info(f"⏳ Saltando por filtros: {c_title}")

        if not info:
            logging.warning(f"⚠️ No se encontraron resultados para '{query}' que pasen los filtros.")
            # Si no hay resultados con filtros, intentamos mostrar qué se encontró aunque se saltara
            return None
        
        logging.info(f"💾 Descargando: {info.get('title')}...")
        filepath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
        if not filepath: return None

        return self._start_playback(info, filepath)

    def _is_too_similar(self, title1, title2, threshold=0.45):
        if not title1 or not title2: return False
        t1, t2 = title1.lower(), title2.lower()
        if self.forced_keyword:
            fk = self.forced_keyword.lower()
            t1 = t1.replace(fk, "").strip()
            t2 = t2.replace(fk, "").strip()
        ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
        return ratio > threshold, ratio

    def update(self):
        try:
            with self._lock:
                if not self.radio_mode or self._paused or self._manually_stopped:
                    return
                if not self._player:
                    return
                    
                # logging.debug("DEBUG: player.update TICK")
                try:
                    pts = self._player.get_pts() or 0
                except (Exception, BaseException) as e:
                    logging.info(f"DEBUG: get_pts() falló (normal en transición): {e}")
                    pts = 0
                
                try:
                    meta = self._player.get_metadata()
                    duration = meta.get('duration', self._current_duration or 0)
                except (Exception, BaseException) as e:
                    # logging.debug(f"DEBUG Error en get_metadata(): {e}")
                    duration = self._current_duration or 0
                
                if duration > 0:
                    rem = duration - pts
                    # Iniciar precarga si falta poco
                    if not self._preloaded_data and not self._preloading and (pts > duration * 0.8 or rem < 20):
                        self._preloading = True
                        logging.info(f"DEBUG: Disparando precarga (PTS: {pts:.1f}, DUR: {duration:.1f})")
                        threading.Thread(target=self._background_preload, daemon=True).start()

                    # Transición a siguiente canción
                    if pts >= duration - 0.8 and duration > 0:
                        logging.info(f"DEBUG: Fin de canción detectado (PTS: {pts:.1f}, DUR: {duration:.1f})")
                        if self._preloaded_data:
                            info, fpath = self._preloaded_data
                            # Limpiar FLAG de precarga ANTES de iniciar el nuevo track para evitar re-entradas curiosas
                            self._preloaded_data = None
                            logging.info(f"DEBUG: Usando dato precargado: {info['title']}")
                            self._start_playback(info, fpath)
                        else:
                            logging.info("DEBUG: Nada precargado, pidiendo next_result")
                            self.next_result(False)
        except (Exception, BaseException) as e:
            logging.error(f"❌ ERROR CRÍTICO en AudioPlayer.update: {e}")
            import traceback
            traceback.print_exc()


    def _background_preload(self):
        try:
            logging.info("⏳ Iniciando precarga JIT...")
            
            # 1. Prioridad: Siguiente en la cola (sin descargar)
            with self._lock:
                if self.queue:
                    for i, item in enumerate(self.queue):
                        info, fpath = item
                        if not fpath:
                            logging.info(f"⏳ Precargando siguiente canción de la cola: {info['title']}")
                            new_fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                            if new_fpath:
                                with self._lock:
                                    # Actualizar en la cola
                                    self.queue[i] = (info, new_fpath)
                                    if i == 0:
                                        self._preloaded_data = (info, new_fpath)
                                logging.info(f"✅ Precarga de cola lista: {info['title']}")
                                return
                            break # Si falla una, paramos o re-intentamos
                    
                    # Si ya están todas las de la cola descargadas, precargamos el primer item en _preloaded_data si no está ya
                    if not self._preloaded_data and self.queue[0][1]:
                        self._preloaded_data = self.queue[0]
                        return

            # 2. Si no hay cola o todo está listo, radio/recs
            res = self._get_next_candidate_data()
            if res: 
                self._preloaded_data = res
                logging.info(f"📦 Canción precargada (Radio): {res[0]['title']}")
            else:
                logging.info("DEBUG: Precarga finalizó sin candidato.")
        except (Exception, BaseException) as e:
            import traceback
            logging.error(f"FATAL en Thread de Precarga: {e}")
            traceback.print_exc()
        finally:
            self._preloading = False


    def _get_next_candidate_data(self):
        logging.info("🔍 Obteniendo siguiente candidato...")
        
        # 1. Prioridad: COLA
        with self._lock:
            if self.queue:
                info, fpath = self.queue.pop(0)
                if not fpath:
                    logging.info(f"⏳ Descargando JIT para el siguiente en cola: {info['title']}")
                    fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                
                if fpath:
                    logging.info(f"✅ Siguiente desde la cola: {info['title']}")
                    return (info, fpath)


        # 2. Modo Playlist de Favoritos
        if self.fav_playlist_mode:
            favs = load_favorites()
            if favs:
                self.fav_index = (self.fav_index + 1) % len(favs)
                chosen = favs[self.fav_index]
                logging.info(f"✅ Siguiente favorito ({self.fav_index+1}/{len(favs)}): {chosen['title']}")
                info = get_search_info(chosen["id"], index=0)
                if info:
                    info["_is_fav_playlist"] = True
                    fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                    if fpath: return (info, fpath)

        # 3. Modo Playlist Importada
        if self.plist_mode:
            all_plist = load_playlists()
            if self.plist_id in all_plist:
                plist_data = all_plist[self.plist_id]
                songs = plist_data.get("songs", [])
                if songs:
                    self.plist_index = (self.plist_index + 1) % len(songs)
                    chosen = songs[self.plist_index]
                    logging.info(f"✅ Siguiente de playlist '{plist_data.get('title')}' ({self.plist_index+1}/{len(songs)}): {chosen['title']}")
                    info = get_search_info(chosen["id"], index=0)
                    if info:
                        info["_is_plist"] = True
                        fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                        if fpath: return (info, fpath)
            else:
                logging.warning(f"⚠️ Playlist activa '{self.plist_id}' no encontrada. Desactivando modo playlist.")
                self.plist_mode = False


        # 4. Modo Radio / Recomendaciones
        if self._current_id:
            recs = get_recommendations(self._current_id)
            logging.info(f"📋 Evaluando {len(recs)} recomendaciones...")
            for entry in recs:
                try:
                    if entry["id"] == self._current_id: continue
                    if entry["title"] in self.history:
                        logging.info(f"⏳ Saltado (Ya escuchado): {entry['title']}")
                        continue
                    
                    if not is_content_allowed(entry, self.config): continue
                    
                    similar, ratio = self._is_too_similar(self._current_title, entry["title"])
                    if similar:
                        logging.info(f"⏳ Saltado (Muy similar [{int(ratio*100)}%]): {entry['title']}")
                        continue
                    
                    logging.info(f"✅ Candidato encontrado (Recs): {entry['title']}")
                    info = get_search_info(entry['id'], index=0)
                    if info:
                        fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                        if fpath: return (info, fpath)
                except Exception as e:
                    logging.error(f"Error evaluando candidato radio: {e}")
                    continue

        if self._last_query:
            logging.info(f"📋 Buscando en resultados de '{self._last_query}'...")
            for i in range(1, 10):
                idx = self._last_index + i
                info = get_search_info(self._last_query, index=idx)
                if not info: break
                
                if info["title"] in self.history:
                    logging.info(f"⏳ Saltado (Ya escuchado): {info['title']}")
                    continue
                
                if not is_content_allowed(info, self.config): continue
                
                similar, ratio = self._is_too_similar(self._current_title, info["title"])
                if similar:
                    logging.info(f"⏳ Saltado (Muy similar [{int(ratio*100)}%]): {info['title']}")
                    continue
                
                logging.info(f"✅ Candidato encontrado (Búsqueda): {info['title']}")
                fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                if fpath: return (info, fpath)
        
        logging.warning("⚠️ No se encontraron candidatos adecuados para la radio.")
        return None

    def next_result(self, open_in_browser: bool):
        # Si ya hay algo precargado, lo usamos
        with self._lock:
            if self._preloaded_data:
                info, fpath = self._preloaded_data
                self._preloaded_data = None
                
                # Si el dato precargado era el que estaba en el tope de la cola, lo quitamos
                if self.queue and self.queue[0][0]["id"] == info["id"]:
                    self.queue.pop(0)

                logging.info(f"⏭️ Usando canción precargada: {info['title']}")
                self._start_playback(info, fpath)
                return

        res = self._get_next_candidate_data()
        if res: self._start_playback(res[0], res[1])


    def pause(self):
        if self._player: self._player.set_pause(True)
        self._paused = True

    def resume(self):
        if self._player: self._player.set_pause(False)
        self._paused = False

    def get_playback_info(self):
        with self._lock:
            title = self._current_title or "Nada sonando"
            r = "ON" if self.radio_mode else "OFF"
            f = "ON" if self.config.get("filters_enabled", True) else "OFF"
            fk = self.forced_keyword or "OFF"
            v_music = int(self._volume * 1000)
            v_tts = int(self.config.get("tts_volume", 1.0) * 100)
            m_status = "ON" if self.config.get("listen_enabled", True) else "OFF"
            
            pos_str = "0:00/0:00"
            if self._player:
                pts = self._player.get_pts() or 0
                dur = self._player.get_metadata().get('duration', self._current_duration or 0)
                def fmt_sec(s): return f"{int(s//60)}:{int(s%60):02d}"
                pos_str = f"   ⏳ {fmt_sec(pts)}/{fmt_sec(dur)}"
    
            next_str = ""
            if self._preloaded_data: 
                next_str = f"Siguiente: {self._preloaded_data[0]['title']}\n"
            elif self.queue:
                next_str = f"Siguiente: {self.queue[0][0]['title']} (⏳ cargando)\n"
            elif self._preloading: 
                next_str = "Siguiente: ⏳ Buscando...\n"
    
            q_str = f" | 📦 Cola: {len(self.queue)}" if self.queue else ""
            p_str = f" | 📂 Playlist: {self.plist_title}" if self.plist_mode and self.plist_title else ""
    
            return (f"🎵 Sonando: {title}\n{pos_str}\n{next_str}"
                    f"| 📻 Radio: {r} | 🛡️ Filtros: {f} | 🎯 Forzar: {fk}{q_str}{p_str}\n"
                    f"| 🔊 Música: {v_music}% | 🗣️ Voz: {v_tts}%\n"

                    f"| 🎤 Micro: {m_status} [{self.config.get('microphone_index', '0')}]")

    def stop_locked(self):
        self._manually_stopped = True
        if self._player:
            logging.info("DEBUG: Deteniendo reproductor anterior...")
            try:
                self._player.close_player()
            except Exception as e:
                logging.debug(f"Error close_player: {e}")
            self._player = None

    def stop(self):
        with self._lock:
            self.stop_locked()

    def set_volume(self, percent: int):
        with self._lock:
            # Ahora permitimos hasta 200 (0.2)
            v = max(0.0, min(0.2, percent / 1000.0))
            self._volume = v
            self.config["volume"] = v
            save_config(self.config)
            if self._player: self._player.set_volume(v)
            logging.info(f"🔊 Volumen: {int(v*1000)}%")

    def add_favorite(self, info):
        if not info: return "No hay canción para añadir"
        favs = load_favorites()
        if any(f["id"] == info["id"] for f in favs):
            return f"'{info['title']}' ya está en favoritos"
        favs.append({"id": info["id"], "title": info["title"]})
        save_favorites(favs)
        return f"Añadido a favoritos: {info['title']}"

    def get_favorites_text(self):
        favs = load_favorites()
        if not favs: return "La lista de favoritos está vacía"
        lines = [f"{i+1}. {f['title']}" for i, f in enumerate(favs)]
        return "\n".join(lines)

    def import_playlist(self, url):
        logging.info(f"📥 Importando playlist desde: {url}")
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'force_generic_extractor': False,
            'nocheckcertificate': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(url, download=False)
                if not res: return "No se pudo extraer información."
                
                # Fallback: Si recibimos un video pero la URL tenía 'list=', intentamos forzar la playlist
                if res.get("_type") != "playlist" and "list=" in url:
                    import re
                    m = re.search(r"[&?]list=([A-Za-z0-9_-]+)", url)
                    if m:
                        plist_id = m.group(1)
                        plist_url = f"https://www.youtube.com/playlist?list={plist_id}"
                        logging.info(f"🔄 Detectado ID de playlist en URL, re-intentando extracción: {plist_url}")
                        res = ydl.extract_info(plist_url, download=False)
                        if not res: return "No se pudo extraer información de la playlist forzada."

                playlist_id = res.get("id")
                playlist_title = res.get("title") or "Playlist Desconocida"
                entries = res.get("entries", [])
                
                if not entries:
                    logging.warning(f"DEBUG: El resultado (tipo {res.get('_type')}) no contiene 'entries'.")
                    return "La playlist parece estar vacía o no es una playlist válida."
                
                if not playlist_id:
                    playlist_id = str(uuid.uuid4())[:8]

                all_playlists = load_playlists()
                
                # Si ya existe, decidimos si fusionar o sobrescribir. Por simplicidad, sobrescribimos/limpiamos
                current_songs = []
                added = 0
                for e in entries:
                    if not e: continue
                    eid = e.get("id")
                    title = e.get("title") or "Sin título"
                    if eid and title:
                        current_songs.append({"id": eid, "title": title})
                        added += 1
                
                all_playlists[playlist_id] = {
                    "title": playlist_title,
                    "songs": current_songs
                }
                
                save_playlists(all_playlists)
                return f"Importación finalizada: '{playlist_title}'. Añadidas {added} canciones (Total en esta lista: {len(current_songs)})."
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error en la importación: {e}"



    def check_playlists(self, query=None, deep=False):
        all_playlists = load_playlists()
        if not all_playlists: return "No hay playlists para verificar."
        
        target_playlists = {}
        if query:
            low_q = query.lower()
            if low_q in all_playlists:
                target_playlists = {low_q: all_playlists[low_q]}
            else:
                for pid, pdata in all_playlists.items():
                    if low_q in pid.lower() or low_q in pdata.get("title", "").lower():
                        target_playlists = {pid: pdata}
                        break
                if not target_playlists:
                    return f"⚠️ No se encontró ninguna playlist que coincida con '{query}'"
        else:
            target_playlists = all_playlists

        total_deleted = 0
        total_songs = sum(len(pdata.get("songs", [])) for pdata in target_playlists.values())
        processed_count = 0
        unavailable_reports = []
        to_delete_map = {} # {pid: [indices_to_remove]}
        recovered_any = False

        if deep:
            logging.info(f"🚀 INICIANDO CHEQUEO PROFUNDO (Deep Recovery Mode) para {len(target_playlists)} playlist(s)...")
        else:
            logging.info(f"🔍 Verificando {len(target_playlists)} playlist(s) ({total_songs} canciones)...")
        
        ydl_opts_flat = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}
        ydl_opts_check = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}

        # Bucle principal
        with YoutubeDL(ydl_opts_check) as ydl:
            for pid, pdata in target_playlists.items():
                p_title = pdata.get("title", pid)
                
                # --- PASO 1: Intentar recuperar títulos vía metadatos de PLAYLIST (Solo en modo normal) ---
                meta_map = {}
                if not deep and len(pid) > 5 and not pid.startswith("migrated"):
                    try:
                        logging.info(f"⏳ Recuperando metadatos de la lista '{p_title}'...")
                        with YoutubeDL(ydl_opts_flat) as ydl_flat:
                            plist_info = ydl_flat.extract_info(f"https://www.youtube.com/playlist?list={pid}", download=False)
                            if plist_info and "entries" in plist_info:
                                for entry in plist_info["entries"]:
                                    if entry and entry.get("id"):
                                        t = entry.get("title")
                                        # Solo guardamos si no es un título genérico de YT
                                        if t and not any(x in t.lower() for x in ["deleted video", "private video", "v\u00eddeo eliminado", "v\u00eddeo privado"]):
                                            meta_map[entry["id"]] = t
                    except: pass
                
                valid_indices = []
                for i, s in enumerate(pdata.get("songs", [])):
                    processed_count += 1
                    s_id = s.get("id")
                    current_title = s.get("title", s_id)
                    
                    # Intentar recuperar si el nombre actual es genérico (borrado/privado/ID)
                    is_generic = any(x in current_title.lower() for x in ["deleted video", "private video", "v\u00eddeo eliminado", "v\u00eddeo privado"]) 
                    if is_generic or current_title == s_id:
                        recovered_title = meta_map.get(s_id)
                        
                        # Fallback 1: Probar extract_flat directo del video
                        if not recovered_title:
                            try:
                                with YoutubeDL(ydl_opts_flat) as ydl_v:
                                    v_info = ydl_v.extract_info(f"https://www.youtube.com/watch?v={s_id}", download=False)
                                    vt = v_info.get("title")
                                    if vt and not any(x in vt.lower() for x in ["deleted video", "private video", "v\u00eddeo eliminado", "v\u00eddeo privado"]):
                                        recovered_title = vt
                            except: pass

                        # Fallback 2: Búsqueda SOS en la web (Google Search vía ydl)
                        # Muchos vídeos borrados tienen el ID indexado con su título original en la web
                        if not recovered_title:
                            try:
                                search_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'skip_download': True}
                                with YoutubeDL(search_opts) as ydl_s:
                                    # Buscamos el ID en Google a través de yt-dlp
                                    search_res = ydl_s.extract_info(f"gvsearch1:{s_id}", download=False)
                                    if search_res and "entries" in search_res and search_res["entries"]:
                                        st = search_res["entries"][0].get("title")
                                        if st and s_id in st: # Si el resultado contiene el ID, puede ser el título real
                                            # Limpiamos el título si viene con formato de búsqueda
                                            clean_st = st.replace(s_id, "").replace(" - YouTube", "").strip(" - ")
                                            if clean_st and len(clean_st) > 3:
                                                recovered_title = clean_st
                            except: pass

                        if recovered_title:
                            s["title"] = f"\u267b\ufe0f {recovered_title} \u267b\ufe0f"
                            recovered_any = True
                            logging.info(f"♻\ufe0f T\u00edtulo recuperado para {s_id}: {recovered_title}")

                    s_title = s.get("title")
                    print(f"\r[{processed_count}/{total_songs}] Verificando: {s_title} (en {p_title})\033[K", end="", flush=True)
                    
                    try:
                        # Verificamos disponibilidad real (si es reproducible)
                        ydl.extract_info(f"https://www.youtube.com/watch?v={s_id}", download=False)
                        valid_indices.append(i)
                    except Exception:
                        total_deleted += 1
                        # En el informe, usamos el título tal cual (que ya podría tener los ♻️)
                        unavailable_reports.append(f"- {s_title} (ID: {s_id}) (Playlist: {p_title})")
                
                if len(valid_indices) != len(pdata.get("songs", [])):
                    all_indices = set(range(len(pdata.get("songs", []))))
                    invalid_indices = all_indices - set(valid_indices)
                    to_delete_map[pid] = sorted(list(invalid_indices), reverse=True)

        # Si recuperamos algo, guardamos los cambios en los títulos inmediatamente
        if recovered_any:
            save_playlists(all_playlists)
            logging.info("\u2705 Se han actualizado los nombres recuperados en el archivo de playlists.")

        print("\n\u2705 Verificaci\u00f3n completada.")
        
        if total_deleted > 0:
            report_msg = "\n".join(unavailable_reports)
            print(f"\n\u26a0\ufe0f Se han detectado {total_deleted} canciones no disponibles:\n{report_msg}")
            
            # Preguntar antes de borrar
            confirm = input("\n\u00bfDeseas eliminar estas canciones de tus playlists? (s/n): ").strip().lower()
            if confirm == 's':
                for pid, indices in to_delete_map.items():
                    orig_songs = all_playlists[pid]["songs"]
                    for idx in indices:
                        orig_songs.pop(idx)
                
                save_playlists(all_playlists)
                return f"He borrado {total_deleted} canciones de tus playlists."
            else:
                return "Operaci\u00f3n cancelada. No se han realizado cambios en la composici\u00f3n de las listas."
        
        return "Todas las canciones verificadas est\u00e1n disponibles."


    def check_favorites(self):
        favs = load_favorites()
        if not favs: return "No hay favoritos para verificar"
        
        logging.info("🔍 Verificando favoritos...")
        deleted = []
        valid = []
        
        ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True}
        with YoutubeDL(ydl_opts) as ydl:
            for f in favs:
                try:
                    ydl.extract_info(f"https://www.youtube.com/watch?v={f['id']}", download=False)
                    valid.append(f)
                except:
                    deleted.append(f["title"])
        
        if deleted:
            save_favorites(valid)
            return f"He borrado {len(deleted)} favoritos no disponibles: " + ", ".join(deleted)
        return "Todos tus favoritos están disponibles"

    def play_random_favorite(self, open_in_browser):
        import random
        favs = load_favorites()
        if not favs: return None
        chosen = random.choice(favs)
        return self.play_query(chosen["id"], open_in_browser)

    # -----------------------------------------------------------
    # UNIFIED COMMAND EXECUTOR
    # -----------------------------------------------------------
    def execute_command(self, cmd, args, voice_loop=None, open_in_browser=False):
        """Hub central para procesar comandos de cualquier interfaz."""
        logging.info(f"DEBUG: Executing unified command: {cmd} {args}")

        if cmd == "help":
            print(AYUDA_MSG)
        
        elif cmd == "play":
            self.play_query(args["query"], open_in_browser)
        
        elif cmd == "toggle":
            if self._player and not self._paused:
                self.pause()
            else:
                self.resume()

        elif cmd == "pause": 
            self.pause()

        elif cmd == "resume": 
            self.resume()

        elif cmd == "stop": 
            self.stop()

        elif cmd == "next": 
            self.next_result(open_in_browser)

        elif cmd == "shuffle":
            with self._lock:
                if not self.queue:
                    print("⚠️ La cola está vacía, nada que mezclar.")
                else:
                    import random
                    random.shuffle(self.queue)
                    # Forzamos reseteo de precarga para que info muestre el nuevo 'Siguiente'
                    self._preloaded_data = None
                    self._preloading = False
                    logging.info("🔀 Cola mezclada aleatoriamente.")

        elif cmd == "history":
            print("\n📜 ÚLTIMAS CANCIONES:")
            for i, t in enumerate(reversed(self.history)):
                print(f"  {i+1}. {t}")

        elif cmd == "add":
            query = args["query"]
            
            # 1. ¿Es una playlist local?
            all_plist = load_playlists()
            target_plist = None
            low_q = query.lower()
            if low_q in all_plist:
                target_plist = all_plist[low_q]
            else:
                for pid, data in all_plist.items():
                    if low_q in pid.lower() or low_q in data.get("title", "").lower():
                        target_plist = data
                        break
            
            if target_plist:
                songs = target_plist.get("songs", [])
                logging.info(f"📂 Encolando {len(songs)} canciones de '{target_plist.get('title')}' (JIT)")
                with self._lock:
                    for s in songs:
                        # Añadimos a la cola con fpath=None para descarga bajo demanda
                        self.queue.append(({"id": s["id"], "title": s["title"]}, None))
                return

            # 2. Búsqueda normal en YouTube
            logging.info(f"➕ Buscando para añadir: {query}")
            def _bg_add():
                info = None
                for i in range(10):
                    candidate = get_search_info(query, index=i)
                    if not candidate: break
                    if is_content_allowed(candidate, self.config):
                        info = candidate
                        break
                
                if info:
                    with self._lock:
                        # Encolar solo info
                        self.queue.append((info, None))
                        
                        # UNIFICACIÓN PSA: Si hay una playlist activa, añadirla también allí
                        if self.plist_mode and self.plist_id:
                            all_plist = load_playlists()
                            if self.plist_id in all_plist:
                                pdata = all_plist[self.plist_id]
                                if not any(s["id"] == info["id"] for s in pdata.get("songs", [])):
                                    pdata.setdefault("songs", []).append({"id": info["id"], "title": info["title"]})
                                    save_playlists(all_plist)
                                    logging.info(f"📂 [Playlist] '{info['title']}' añadido a '{pdata['title']}'")

                    logging.info(f"✅ [JIT] Añadido a la cola: {info['title']}")
                else:
                    logging.warning(f"⚠️ No se encontró nada para: {query}")
            
            threading.Thread(target=_bg_add, daemon=True).start()


        elif cmd == "volume": 
            self.set_volume(args["n"])

        elif cmd == "volume_rel":
            current = int(self._volume * 1000)
            change = 10 if args["direction"] == "up" else -10
            new_vol = max(0, min(200, current + change))
            self.set_volume(new_vol)
        
        elif cmd == "mute":
            self._saved_vol = self._volume
            self.set_volume(0)
        
        elif cmd == "unmute":
            vol = getattr(self, '_saved_vol', 0.05)
            self.set_volume(int(vol * 1000))

        elif cmd == "replay":
            if self._current_info and self._current_filepath:
                self._start_playback(self._current_info, self._current_filepath)

        elif cmd == "fav":
            res = self.add_favorite(self._current_info)
            logging.info(f"⭐️ {res}")

        elif cmd == "favlast":
            res = self.add_favorite(self._previous_info)
            logging.info(f"⭐️ {res}")

        elif cmd == "favlist":
            text = self.get_favorites_text()
            print(f"\n⭐ MIS FAVORITOS:\n{text}")

        elif cmd == "playfav":
            favs = load_favorites()
            if not favs: return
            self.fav_playlist_mode = True
            self.fav_index = 0
            # Empezamos con el primero
            info = get_search_info(favs[0]["id"], index=0)
            if info:
                info["_is_fav_playlist"] = True
                fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                if fpath: self._start_playback(info, fpath)

        elif cmd == "favcheck":
            res = self.check_favorites()
            logging.info(f"🔍 {res}")

        elif cmd == "import":
            res = self.import_playlist(args["url"])
            logging.info(f"📥 {res}")

        elif cmd == "playlist":
            all_plist = load_playlists()
            if not all_plist:
                print("⚠️ No hay playlists guardadas. Usa 'import {url}' primero.")
                return
                
            query = args.get("q")
            if not query:
                print("\n📁 PLAYLISTS DISPONIBLES:")
                for pid, data in all_plist.items():
                    print(f"  - [{pid}] {data.get('title')} ({len(data.get('songs',[]))} canciones)")
                print("\nUsa 'pp [nombre/id]' para reproducir una.")
                return

            # Buscar mejor coincidencia
            target_id = None
            low_q = query.lower()
            if low_q in all_plist:
                target_id = low_q
            else:
                for pid, data in all_plist.items():
                    if low_q in pid.lower() or low_q in data.get("title", "").lower():
                        target_id = pid
                        break
            
            if not target_id:
                print(f"⚠️ No se encontró ninguna playlist que coincida con '{query}'")
                return

            plist_data = all_plist[target_id]
            songs = plist_data.get("songs", [])
            if not songs:
                print(f"⚠️ La playlist '{plist_data.get('title')}' está vacía.")
                return

            self.plist_mode = True
            self.fav_playlist_mode = False
            self.plist_id = target_id
            self.plist_title = plist_data.get("title")
            
            # Limpiar cola actual para priorizar la playlist
            with self._lock:
                self.queue.clear()
            
            logging.info(f"▶️ Reproduciendo playlist vía Cola: {self.plist_title}")
            
            # Reproducir la primera canción inmediatamente
            first_song = songs[0]
            info = get_search_info(first_song["id"], index=0)
            if info:
                info["_is_plist"] = True
                fpath = download_media(info.get("page_url") or info.get("url"), prefix=LOCAL_PREFIX)
                if fpath: 
                    self._start_playback(info, fpath)
                    
                    # Encolar el resto como metadatos (JIT)
                    remaining = songs[1:]
                    if remaining:
                        with self._lock:
                            for s in remaining:
                                self.queue.append(({"id": s["id"], "title": s["title"]}, None))
                        logging.info(f"📂 {len(remaining)} canciones restantes de '{self.plist_title}' encoladas (JIT)")


            
        elif cmd == "playlists":
            all_plist = load_playlists()
            if not all_plist:
                print("\n⚠️ No hay playlists guardadas.")
            else:
                print("\n📁 TUS PLAYLISTS:")
                for pid, data in all_plist.items():
                    print(f"  - [{pid}] {data.get('title')} ({len(data.get('songs',[]))} canciones)")
                print("\nUsa 'pp [nombre]' para sonar una, o 'add [nombre]' para encolar.")

        elif cmd == "playlist_remove":
            query = args.get("q")
            if not query:
                print("⚠️ Especifica el nombre de la playlist a eliminar.")
                return
            
            all_plist = load_playlists()
            target_id = None
            low_q = query.lower()
            if low_q in all_plist:
                target_id = low_q
            else:
                for pid, data in all_plist.items():
                    if low_q in pid.lower() or low_q in data.get("title", "").lower():
                        target_id = pid
                        break
            
            if not target_id:
                print(f"⚠️ No se encontró ninguna playlist que coincida con '{query}'")
                return
            
            p_title = all_plist[target_id].get("title", target_id)
            confirm = input(f"¿Seguro que quieres eliminar la playlist '{p_title}'? (s/n): ").strip().lower()
            if confirm == 's':
                del all_plist[target_id]
                save_playlists(all_plist)
                if self.plist_id == target_id:
                    self.plist_mode = False
                    self.plist_id = None
                print(f"✅ Playlist '{p_title}' eliminada.")
            else:
                print("Operación cancelada.")

        elif cmd == "playlistcheck":
            res = self.check_playlists(args.get("q"))
            logging.info(f"🔍 {res}")

        elif cmd == "playlistcheck_deep":
            res = self.check_playlists(args.get("q"), deep=True)
            logging.info(f"🔍 {res}")

        elif cmd == "radio": self.toggle_radio(args["enabled"])
        elif cmd == "filtros": self.toggle_filters(args["enabled"])
        elif cmd == "info": print(self.get_playback_info())
        
        elif cmd == "force":
            kw = self.set_forced_filter(args["f"])
            if kw: self.play_query(kw, open_in_browser)

        elif cmd == "listen":
            enabled = args["enabled"]
            self.config["listen_enabled"] = enabled
            save_config(self.config)
            status = "activado" if enabled else "desactivado"
            logging.info(f"🎤 Micrófono {status}")

        elif cmd == "exit":
            self.stop()
            print("\n👋 ¡Hasta pronto!")
            os._exit(0)

        elif cmd == "set_mic":
            idx = resolve_mic_index(args["n"])
            self.config["microphone_index"] = idx
            save_config(self.config)

        elif cmd == "list_mics":
             list_microphones()

# -----------------------------------------------------------
# Bucle de voz
# -----------------------------------------------------------
class VoiceLoop:
    def __init__(self, player: AudioPlayer, open_in_browser: bool = False, hotword: str | None = None):
        self.player = player
        self.open_in_browser = open_in_browser
        self.parser = CommandParser()
        if isinstance(hotword, str): self.hotword = [hotword.lower()]
        elif isinstance(hotword, list): self.hotword = [h.lower() for h in hotword if isinstance(h, str)]
        else: self.hotword = None
        self._stop_flag = False
        
        pref_mic = self.player.config.get("microphone_index")
        self.mic_index = resolve_mic_index(pref_mic)
        logging.info(f"🎤 Micrófono activo: {self.mic_index}")

        self._recognizer = sr.Recognizer()
        self._recognizer.dynamic_energy_threshold = True


    def _listen_once(self) -> str | None:
        with sr.Microphone(device_index=self.mic_index) as source:
            self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
            try:
                audio = self._recognizer.listen(source, timeout=5, phrase_time_limit=8)
                text = self._recognizer.recognize_google(audio, language="es-ES")
                return text
            except: return None

    def run(self):
        logging.info("\n" + "="*50 + "\nVTM LISTO - ESTADO INICIAL:\n" + self.player.get_playback_info() + "\n" + "="*50)
        
        while not self._stop_flag:
            try:
                time.sleep(0.5)
                # Eliminamos self.player.update() de aquí porque ya hay un update_loop independiente
                if not self.player.config.get("listen_enabled", True):
                    time.sleep(1)
                    continue
                    
                text = self._listen_once()
                if not text:
                    continue
                
                logging.info(f"🗣️ [VTM Escuchó]: '{text}'")
                
                # Usamos el texto original para preservar mayúsculas (URLs!)
                raw_text = text.strip()
                t_low = raw_text.lower()
                self.hotword = self.player.config.get("hotwords", ["rafa"])
                
                detected_hw = None
                for hw in self.hotword:
                    if hw.lower() in t_low:
                         detected_hw = hw.lower()
                         break
                
                if self.hotword and not detected_hw: continue 
                
                command_text = raw_text
                if detected_hw:
                    # Buscamos dónde termina el hotword en el texto original (case-insensitive)
                    idx = t_low.find(detected_hw)
                    command_text = raw_text[idx + len(detected_hw):].strip(" .,-!¡?¿")
                    if not command_text:
                        continue

                cmd, args = self.parser.parse(command_text)
                if not cmd: continue
                self._exec_cmd(cmd, args)

            except Exception as e:
                import traceback
                logging.error(f"Error en VoiceLoop: {e}")
                traceback.print_exc()
                time.sleep(1)

    def _exec_cmd(self, cmd, args):
        self.player.execute_command(cmd, args, voice_loop=self, open_in_browser=self.open_in_browser)


def cli_loop(player: AudioPlayer, open_in_browser: bool, hotword: str | None, voice_loop: 'VoiceLoop' = None):
    parser = CommandParser()
    while True:
        try:
            line = input("> ").strip()
            if not line: continue
            
            # Procesamos el hotword de forma case-insensitive pero preservamos el resto
            if hotword:
                low_line = line.lower()
                low_hw = hotword.lower()
                if low_line.startswith(low_hw):
                    line = line[len(low_hw):].strip()
            
            cmd, args = parser.parse(line)
            if not cmd:
                # logging.debug(f"DEBUG: No se reconoció comando en: '{line}'")
                continue
            
            player.execute_command(cmd, args, voice_loop=voice_loop, open_in_browser=open_in_browser)

        except (KeyboardInterrupt, EOFError):
            print("\n👋 ¡Hasta pronto! (Interrumpido)")
            player.stop()
            os._exit(0)
        except Exception as e:
            logging.error(f"Error en el prompt de comandos: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(0.5)

def check_for_updates():
    try:
        logging.info(f"🔍 Comprobando actualizaciones... (Versión actual: {VTM_VERSION})")
        # Usamos un User-Agent para evitar bloqueos básicos
        req = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            content = response.read().decode('utf-8')
            # Buscar VTM_VERSION = "x.x.x" en el contenido remoto
            match = re.search(r'VTM_VERSION\s*=\s*["\']([^"\']+)["\']', content)
            if match:
                remote_version = match.group(1)
                if remote_version != VTM_VERSION:
                    print("\n" + "!" * 60)
                    print(f"🚀 ¡NUEVA VERSIÓN DISPONIBLE EN GITHUB! ({remote_version})")
                    print(f"   Versión instalada: {VTM_VERSION}")
                    print(f"   Link: https://github.com/Cicker21/VTM")
                    print("!" * 60 + "\n")
                    time.sleep(2)
                else:
                    logging.info("✅ VTM está actualizado.")
    except Exception as e:
        logging.warning(f"⚠️ No se pudo comprobar actualizaciones: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--texto", action="store_true")
    ap.add_argument("--navegador", action="store_true")
    ap.add_argument("--radio-init", choices=["on", "off"], default="on")
    ap.add_argument("--micro-init", choices=["on", "off"], default=None)
    ap.add_argument("--listar-micros", action="store_true")
    args = ap.parse_args()

    if args.listar_micros:
        list_microphones()
        return



    cleanup_temp_files(prefix=LOCAL_PREFIX)
    check_for_updates()
    player = AudioPlayer(radio_enabled=(args.radio_init == "on"))
    
    # Sobrescribir micro si se pasa por CLI
    if args.micro_init:
        player.config["listen_enabled"] = (args.micro_init == "on")
        # No guardamos al config.json aquí para que el CLI sea temporal
    
    # Hotwords now return a list from vtm_core
    hotwords = player.config.get("hotwords", ["rafa"])

    # --- Background Update Thread ---
    def update_loop():
        logging.info("DEBUG: update_loop iniciado.")
        try:
            while True:
                player.update()
                time.sleep(0.5)
        except BaseException as e:
            logging.error(f"DEBUG: update_loop CRASH: {e}")
            import traceback
            traceback.print_exc()
        finally:
            logging.info("DEBUG: update_loop finalizado.")

    threading.Thread(target=update_loop, daemon=True).start()

    # --- Start Voice Loop in Thread ---
    v_loop = VoiceLoop(player, args.navegador, hotwords)
    threading.Thread(target=v_loop.run, daemon=True).start()

    # --- Main Thread: CLI Loop (Always active now) ---
    cli_loop(player, args.navegador, hotwords[0] if hotwords else None, voice_loop=v_loop)

if __name__ == "__main__":
    try:
        main()
        logging.info("DEBUG: main() ha retornado normalmente.")
    except (Exception, BaseException) as e:
        import traceback
        # Si es una interrupción normal (Ctrl+C), no mostrar error fatal
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            logging.info(f"👋 Cerrando asistente ({type(e).__name__})...")
        else:
            logging.error(f"FATAL ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
            print("\n" + "="*50)
            print("El programa se ha detenido debido a un error crítico.")
            input("Presiona ENTER para cerrar esta ventana...")
    finally:
        logging.info("🏁 Saliendo de vtm.py (block finally)")
