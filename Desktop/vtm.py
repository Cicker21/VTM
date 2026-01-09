# -*- coding: utf-8 -*-
"""
vtm.py — Reproducir música de YouTube mediante comandos de voz o texto (ES)
"""

VTM_VERSION = "0.10.3"
UPDATE_URL = "https://raw.githubusercontent.com/Cicker21/VTM/refs/heads/main/Desktop/vtm.py"

import argparse
import logging
import os
import time
import difflib
import threading
import speech_recognition as sr
import json
import uuid
import re
import atexit
import urllib.request
import random
import concurrent.futures

from yt_dlp import YoutubeDL
from ffpyplayer.player import MediaPlayer
import urllib.request
import urllib.error

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
    "- pc / pcr / pcd / pcdr [q] Verificar playlists (Normal / Recov / Deep / DeepRecov)\n\n"
    
    "⭐️ FAVORITOS\n"
    "- fav / me gusta     Guardar actual en favoritos\n"
    "- favlast            Guardar la anterior en favoritos\n"
    "- fp / playfav       Reproducir tus favoritos\n"
    "- fr / favrandom     Modo aleatorio de favoritos\n"
    "- favlist            Listar todos tus favoritos\n"
    "- favcheck           Verificar disponibilidad de favoritos\n\n"
    
    "⚙️ AJUSTES Y SISTEMA\n"
    "- ensure [id]        Diagnóstico forzado de un ID\n"
    "- info / estado      ¿Qué está sonando?\n"
    "- radio [on/off]     Modo radio al vaciarse la cola\n"
    "- con/sin filtros    Activar/Quitar filtros de YouTube\n"
    "- forzar [palabra]   Filtrar radio por una palabra clave\n"
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
        logging.error(f"yt-dlp: {msg}")

def _get_ytdl_opts(download=False, playlist_items=None, quiet=True, outtmpl=None):
    opts = {
        'format': 'bestaudio/best',
        'quiet': quiet,
        'no_warnings': True,
        'logger': YtdlLogger(),
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    }
    if not download:
        opts.update({'extract_flat': True, 'lazy_playlist': True})
    if playlist_items:
        opts['playlist_items'] = playlist_items
    if outtmpl:
        opts['outtmpl'] = outtmpl
    return opts

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
    if not config.get("filters_enabled", True): return True
    t, d = info.get("title", "").lower(), info.get("duration", 0)
    
    f = config.get("forced_keyword")
    if f and f.lower() not in t: return False
    if any(w.lower() in t for w in config.get("blacklisted_keywords", [])): return False
    if d > config.get("max_duration_seconds", 600): return False
    
    is_short = any(k.lower() in t for k in config.get("shorts_keywords", ["#shorts", "shorts", "reels"]))
    if is_short and d <= config.get("max_shorts_duration", 65): return False
    
    return info.get("_type", "video") in ["video", "url", "url_transparent"]

def get_search_info(query: str, index: int = 0):
    is_url = query.startswith("http")
    search_query = query if is_url else f"ytsearch10:{query}"
    opts = _get_ytdl_opts(playlist_items='1-10')
    
    try:
        with YoutubeDL(opts) as ydl:
            res = ydl.extract_info(search_query, download=False)
            if not res: return None
            
            entries = res.get("entries", [])
            if not entries and "title" in res:
                if "url" not in res and "webpage_url" in res: res["url"] = res["webpage_url"]
                return res
            
            if index < len(entries):
                entry = entries[index]
                e_url = entry.get("url") or entry.get("webpage_url")
                e_type = entry.get("_type", "video")

                if not entry.get("duration") and e_url and e_type in ["url", "url_transparent"]:
                    with YoutubeDL(opts) as ydl2:
                        full_info = ydl2.extract_info(e_url, download=False)
                        if full_info and full_info.get("_type") == "playlist":
                            full_info["_type"] = "playlist"
                        return full_info
                return entry
    except Exception as e:
        logging.error(f"Error búsqueda: {e}")
    return None

def download_media(url, prefix=TEMP_AUDIO_PREFIX):
    if not url: return None
    filepath = os.path.join(_SCRIPT_DIR, f"{prefix}{uuid.uuid4()}.m4a")
    opts = _get_ytdl_opts(download=True, outtmpl=filepath)
    try:
        with YoutubeDL(opts) as ydl:
            if ydl.download([url]) == 0 and os.path.exists(filepath):
                return filepath
    except Exception as e:
        logging.error(f"Error descarga: {e}")
    return None

def get_recommendations(video_id: str):
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = _get_ytdl_opts(playlist_items='1-15')
    try:
        with YoutubeDL(opts) as ydl:
             info = ydl.extract_info(url, download=False)
             return [{"id": e["id"], "title": e["title"], "duration": e.get("duration")} for e in info.get("entries", []) if e]
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
    RE_FAV_RANDOM = re.compile(r"^(fr|favrandom)$", re.I)
    RE_IMPORT = re.compile(r"^(import|importar)\s+(?P<url>https?://[^\s]+)$", re.I)
    RE_PLAYLIST = re.compile(r"^(pp|playlist|lista)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_REMOVE = re.compile(r"^(pr|ppremove|playlistremove)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLISTS = re.compile(r"^(ps|playlists)$", re.I)
    RE_PLAYLIST_CHECK = re.compile(r"^(pc|playlistcheck|playlist check|checkplaylist)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_CHECK_RECOVERED = re.compile(r"^(pcr)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_CHECK_DEEP = re.compile(r"^(pcd|deepcheck)(\s+(?P<q>.+))?$", re.I)
    RE_PLAYLIST_CHECK_DEEP_RECOVERED = re.compile(r"^(pcdr)(\s+(?P<q>.+))?$", re.I)


    
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
    RE_ENSURE = re.compile(r"^ensure\s+(?P<id>[a-zA-Z0-9_-]{11})$", re.I)

    def parse(self, text: str):
        raw = text.strip()
        t = raw.lower()

        if self.RE_AYUDA.search(raw): return ("help", {})
        if self.RE_PLAYFAV.search(raw): return ("playfav", {}) # Prioridad sobre 'play'
        if self.RE_FAV_RANDOM.search(raw): return ("favrandom", {})
        m = self.RE_PLAYLIST.search(raw)
        if m: return ("playlist", {"q": m.group("q")})

        m = self.RE_PLAYLIST_REMOVE.search(raw)
        if m: return ("playlist_remove", {"q": m.group("q")})

        if self.RE_PLAYLISTS.search(raw): return ("playlists", {})
        m = self.RE_PLAYLIST_CHECK_DEEP.match(t)
        if m: return "playlistcheck_deep", m.groupdict()
        
        m = self.RE_PLAYLIST_CHECK_DEEP_RECOVERED.match(t)
        if m: return "playlistcheck_deep_recovered", m.groupdict()

        m = self.RE_PLAYLIST_CHECK_RECOVERED.match(t)
        if m: return "playlistcheck_recovered", m.groupdict()

        m = self.RE_PLAYLIST_CHECK.match(t)
        if m: return "playlistcheck", m.groupdict()
        
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

        m = self.RE_ENSURE.search(raw)
        if m: return ("ensure", {"id": m.group("id")})

        return (None, {})

# Configuración de logging global
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def on_exit_hook():
    logging.info("🛑 [VTM EXIT HOOK] El proceso está terminando...")

atexit.register(on_exit_hook)

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
        self.plist_mode = False
        self.plist_id = None
        self.plist_title = None
        self.plist_index = 0
        
        self._preloaded_data = None
        self._preloading = False
        self.queue = []
        self._lock = threading.RLock()
        
        # Radio exhaustion prevention
        self._radio_exhausted = False
        self._last_radio_attempt = 0


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
                # Usamos filtro afade para evitar el "blast" inicial
                # volume se setea luego, pero el fade in garantiza silencio al principio
                ff_opts = {'vn': True, 'af': 'afade=t=in:ss=0:d=1'}
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
                time.sleep(0.1)
                
                # Seteamos el volumen objetivo directamente
                if self._player:
                    try:
                        # Primero volumen, luego Despausar
                        logging.info(f"DEBUG: Setting initial volume to {self._volume}")
                        self._player.set_volume(self._volume)
                        
                        # Pequeña espera para asegurar que el volumen se aplicó antes de soltar el audio
                        time.sleep(0.1) 
                        
                        self._player.set_pause(False)
                        logging.info("DEBUG: Player unpaused with fade-in.")
                    except Exception as ep:
                        logging.error(f"DEBUG: Error starting player: {ep}")
                else:
                    logging.error("DEBUG: self._player is None after init!")
                
                # Forzamos el volumen una vez mas por seguridad en hilo aparte
                def force_volume():
                    time.sleep(0.5)
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


    def play_query(self, query: str, index: int = 0):
        # logging.info(f"DEBUG: play_query called with query='{query}', index={index}")
        self.fav_playlist_mode = False # Reset playlist
        self.plist_mode = False
        self._last_query = query
        self._last_index = index


        # Buscamos un resultado que pase los filtros
        info = None
        for i in range(10):
            candidate = get_search_info(query, index=index + i)
            if not candidate: break
            
            c_title = candidate.get("title", "Sin título")
            
            # Si es el MISSISMO video (ID), saltamos
            if self._current_id and candidate.get("id") == self._current_id:
                logging.info(f"⏳ Saltando (Mismo ID): {c_title}")
                continue

            # Si es el MISSISMO video (ID), saltamos
            if self._current_id and candidate.get("id") == self._current_id:
                logging.info(f"⏳ Saltando (Mismo ID): {c_title}")
                continue

            # Si ya está sonando algo muy similar, lo saltamos para buscar el siguiente de Metrika, etc.
            if self._current_title:
                similar, _ = self._is_too_similar(self._current_title, c_title)
                if similar:
                    logging.info(f"⏳ Saltando (Demasiado similar): {c_title}")
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
        filepath = download_media(info.get("page_url") or info.get("url"), prefix=TEMP_AUDIO_PREFIX)
        if not filepath: return None

        # Resetear bandera de radio exhausta al reproducir nueva canción manualmente
        self._radio_exhausted = False
        return self._start_playback(info, filepath)

    def _is_too_similar(self, title1, title2, threshold=0.85):
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
                if self._paused or self._manually_stopped or not self._player: return
                
                try:
                    pts = self._player.get_pts() or 0
                    meta = self._player.get_metadata()
                    duration = meta.get('duration', self._current_duration or 0)
                except:
                    pts, duration = 0, self._current_duration or 0
                
                if duration > 0:
                    rem = duration - pts
                    if not self._preloaded_data and not self._preloading and (pts > duration * 0.8 or rem < 20):
                        self._preloading = True
                        threading.Thread(target=self._background_preload, daemon=True).start()

                    if pts >= duration - 0.8:
                        if self._preloaded_data:
                            info, fpath = self._preloaded_data
                            self._preloaded_data = None
                            self._radio_exhausted = False
                            self._start_playback(info, fpath)
                        elif time.time() - getattr(self, '_last_next_call', 0) > 2:
                            self._last_next_call = time.time()
                            self.next_result()
        except Exception as e:
            logging.error(f"Error update: {e}")


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
                            new_fpath = download_media(info.get("page_url") or info.get("url"), prefix=TEMP_AUDIO_PREFIX)
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


    def _try_candidate(self, info, strict=True):
        if not info: return None
        if strict:
            if info["title"] in self.history: return None
            if not is_content_allowed(info, self.config): return None
            if self._is_too_similar(self._current_title, info["title"])[0]: return None
        
        fpath = download_media(info.get("page_url") or info.get("url"), prefix=TEMP_AUDIO_PREFIX)
        return (info, fpath) if fpath else None


    def _get_next_candidate_data(self):
        logging.info("🔍 Obteniendo siguiente candidato...")
        
        # 1. Prioridad: COLA
        with self._lock:
            # Bucle para saltar canciones fallidas
            while self.queue:
                info, fpath = self.queue.pop(0)
                if not fpath:
                    logging.info(f"⏳ Descargando JIT para el siguiente en cola: {info['title']}")
                    # Si no tiene URL, hay que buscarla primero
                    if not info.get("url") and not info.get("page_url"):
                        full_info = get_search_info(info["id"])
                        if full_info: info.update(full_info)
                    
                    fpath = download_media(info.get("page_url") or info.get("url"), prefix=TEMP_AUDIO_PREFIX)
                
                if fpath: return (info, fpath)
                logging.warning(f"⚠️ No se pudo reproducir '{info.get('title')}' de la cola. Saltando al siguiente...")
        
        # 2. Modo Playlist
        if self.plist_mode:
            all_plist = load_playlists()
            if self.plist_id in all_plist:
                songs = all_plist[self.plist_id].get("songs", [])
                if songs:
                    self.plist_index = (self.plist_index + 1) % len(songs)
                    chosen = songs[self.plist_index]
                    logging.info(f"✅ Siguiente de playlist '{all_plist[self.plist_id].get('title')}' ({self.plist_index+1}/{len(songs)})")
                    # En modo playlist NO somos estrictos con historial/filtros (strict=False)
                    res = self._try_candidate(get_search_info(chosen["id"]), strict=False)
                    if res:
                        res[0]["_is_plist"] = True
                        return res
            else:
                logging.warning(f"⚠️ Playlist '{self.plist_id}' no encontrada. Off."); self.plist_mode = False

        # 3. Modo Radio
        if not self.radio_mode: return None

        # 3.1 Recomendaciones
        if self._current_id:
            logging.info(f"📋 Evaluando recomendaciones para {self._current_id}...")
            for entry in get_recommendations(self._current_id):
                res = self._try_candidate(get_search_info(entry['id']))
                if res: return res

        # 3.2 Última búsqueda
        if self._last_query:
            logging.info(f"📋 Buscando en resultados de '{self._last_query}'...")
            for i in range(1, 10):
                info = get_search_info(self._last_query, index=self._last_index + i)
                if not info: break
                res = self._try_candidate(info)
                if res: return res
        
        # 3.3 Favorito aleatorio
        favs = load_favorites()
        if favs:
            logging.info("🎲 Buscando favorito aleatorio...")
            for _ in range(min(5, len(favs))):
                chosen = random.choice(favs)
                res = self._try_candidate(get_search_info(chosen["id"]))
                if res:
                    logging.info(f"🎲 Reiniciando radio con favorito: {res[0]['title']}")
                    return res

        # 3.4 Artista
        if self._current_title:
            artist = self._current_title.split('-')[0].split('(')[0].strip()
            if len(artist) > 3:
                logging.info(f"🔍 Buscando más de '{artist}'...")
                res = self._try_candidate(get_search_info(artist))
                if res: return res
        
        logging.warning("⚠️ No se encontraron candidatos adecuados para la radio.")
        return None

    def next_result(self):
        # Si ya hay algo precargado, lo usamos
        with self._lock:
            if self._preloaded_data:
                info, fpath = self._preloaded_data
                self._preloaded_data = None
                
                # Si el dato precargado era el que estaba en el tope de la cola, lo quitamos
                if self.queue and self.queue[0][0]["id"] == info["id"]:
                    self.queue.pop(0)

                logging.info(f"⏭️ Usando canción precargada: {info['title']}")
                self._radio_exhausted = False  # Resetear si encontramos contenido
                self._start_playback(info, fpath)
                return

        res = self._get_next_candidate_data()
        if res:
            self._radio_exhausted = False  # Resetear si encontramos contenido
            self._start_playback(res[0], res[1])
        else:
            # Marcar como exhausta para evitar spam
            self._radio_exhausted = True
            logging.warning("⚠️ Radio exhausta: no se encontró ninguna canción nueva. Esperando 30s...")


    def pause(self):
        if self._player: self._player.set_pause(True)
        self._paused = True

    def resume(self):
        if self._player: self._player.set_pause(False)
        self._paused = False

    def _fmt_title(self, info):
        if not info: return "Nada sonando"
        title = info.get("title", info.get("id", "??"))
        # Limpiamos posibles emojis residuales si los hubiera
        title = re.sub(r"[♻️🔎🆘\u267b\ufe0f]", "", title).strip()
        method = info.get("recovery_method")
        if method:
            icon = "♻️" if method == "meta" else ("🔎" if method == "flat" else "🆘")
            return f"{icon} {title} {icon}"
        return title

    def get_playback_info(self):
        with self._lock:
            title = self._fmt_title(self._current_info)
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
                next_str = f"Siguiente: {self._fmt_title(self._preloaded_data[0])}\n"
            elif self.queue:
                next_str = f"Siguiente: {self._fmt_title(self.queue[0][0])} (⏳ cargando)\n"
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
        ydl_opts = _get_ytdl_opts(quiet=True)
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
                
                # Gestión de colisiones y fusión (Merge)
                if playlist_id in all_playlists:
                    old_data = all_playlists[playlist_id]
                    old_songs = old_data.get("songs", [])
                    old_map = {s["id"]: s for s in old_songs}
                    
                    new_ids = {e.get("id") for e in entries if e and e.get("id")}
                    orphaned = [s for s in old_songs if s["id"] not in new_ids]
                    
                    keep_orphans = False
                    if orphaned:
                        print(f"\n❓ Conflict detected: '{playlist_title}' ({playlist_id}) already exists.")
                        ans = input(f"   Se han detectado {len(orphaned)} canciones locales que ya no están en YouTube.\n   ¿Deseas conservar las viejas canciones? (s/n): ").lower()
                        keep_orphans = (ans == 's')

                    # Construir lista combinada
                    merged_songs = []
                    added_new = 0
                    preserved = 0
                    
                    for e in entries:
                        if not e: continue
                        eid = e.get("id")
                        if not eid: continue
                        
                        if eid in old_map:
                            # CONSERVAR EL TÍTULO ANTERIOR (y sus etiquetas de recuperación)
                            merged_songs.append(old_map[eid])
                            preserved += 1
                        else:
                            # Nueva canción de YouTube
                            title = e.get("title") or "Sin título"
                            merged_songs.append({"id": eid, "title": title})
                            added_new += 1
                    
                    if keep_orphans:
                        merged_songs.extend(orphaned)
                        logging.info(f"➕ Se han conservado {len(orphaned)} canciones antiguas.")
                    
                    current_songs = merged_songs
                    msg = f"Importación fusionada: '{playlist_title}'. Nuevas: {added_new}, Preservadas: {preserved}, Total: {len(current_songs)}."
                else:
                    # Importación limpia (nueva playlist)
                    current_songs = []
                    for e in entries:
                        if not e: continue
                        eid = e.get("id")
                        title = e.get("title") or "Sin título"
                        if eid:
                            current_songs.append({"id": eid, "title": title})
                    msg = f"Importación finalizada: '{playlist_title}'. Añadidas {len(current_songs)} canciones."

                all_playlists[playlist_id] = {
                    "title": playlist_title,
                    "songs": current_songs
                }
                
                save_playlists(all_playlists)
                return msg
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error en la importación: {e}"



    def _is_title_generic(self, title):
        if not title: return True
        return any(x in title.lower() for x in ["deleted video", "private video", "v\u00eddeo eliminado", "v\u00eddeo privado", "wayback machine", "internet archive"])

    def _rescue_id(self, s_id, meta_map=None, verbose=True, logger_func=None, sos_only=False):
        """
        Unified engine to rescue a video title from an unavailable ID.
        Priority: Meta (if provided) > Flat > WayBack > SOS Search.
        If sos_only=True, skips Meta and Flat.
        Returns: (recovered_title, method_label) or (None, None)
        """
        if logger_func is None: logger_func = logging.info
        if verbose: logger_func(f"    🔎 Buscando rescate para {s_id}...")
        
        if not sos_only:
            # 1. Meta (Playlist-based or passed map)
            if meta_map and s_id in meta_map:
                t = meta_map[s_id]
                if t and not self._is_title_generic(t):
                    if verbose: logger_func(f"    ♻️ [meta] Encontrado: {t}")
                    return t, "meta"
            
            # 2. Flat Extraction (Direct from YouTube)
            if verbose: logger_func(f"    ⏳ [flat] Intentando extracción directa...")
            # Silenciamos errores de yt-dlp aquí para que no ensucien la consola si falla el Flat
            ydl_opts = _get_ytdl_opts(quiet=True)
            ydl_opts.update({"logger": None, "no_warnings": True, "quiet": True})
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={s_id}", download=False)
                    t = info.get("title")
                    if t and not self._is_title_generic(t):
                        if verbose: logger_func(f"    🔎 [flat] Encontrado: {t}")
                        return t, "flat"
            except: pass
        else:
            if verbose: logger_func(f"    🚀 [pcd] Saltando Meta/Flat, modo SOS-ONLY activado.")
        
        # 3. WayBack Machine (Optimizado con urllib + regex + fallback ytdl)
        # Probamos variantes de URL comunes en YT para aumentar éxito en WayBack
        target_urls = [
            f"https://www.youtube.com/watch?v={s_id}",
            f"https://youtu.be/{s_id}",
            f"https://www.youtube.com/v/{s_id}"
        ]
        
        for base_url in target_urls:
            try:
                wb_api = f"https://archive.org/wayback/available?url={base_url}"
                if verbose: logger_func(f"    ⏳ [sos(wayback)] Consultando archivo ({base_url.split('/')[-1]})...")
                with urllib.request.urlopen(wb_api, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    snap = data.get("archived_snapshots", {}).get("closest")
                    if snap and snap.get("available"):
                        snap_url = snap["url"]
                        
                        # Intento 1: Extracción rápida vía urllib (más eficiente)
                        try:
                            with urllib.request.urlopen(snap_url, timeout=5) as page:
                                html = page.read().decode('utf-8', errors='replace')
                                match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
                                if match:
                                    wt = match.group(1).replace(" - YouTube", "").replace("YouTube", "").strip()
                                    wt = wt.replace("&quot;", "\"").replace("&#39;", "'").replace("&amp;", "&")
                                    if wt and len(wt) > 5 and not self._is_title_generic(wt):
                                        if verbose: logger_func(f"    🆘 [sos(wayback)] Encontrado (urllib): {wt}")
                                        return wt, "sos(wayback)"
                        except: pass
                        
                        # Intento 2: Fallback a yt-dlp (más pesado pero entiende mejor el HTML de memento)
                        try:
                            if verbose: logger_func(f"    ⏳ [sos(wayback)] Fallback a yt-dlp para snapshot...")
                            with YoutubeDL(ydl_opts) as ydl_wb:
                                wb_info = ydl_wb.extract_info(snap_url, download=False)
                                wt = wb_info.get("title")
                                if wt:
                                    clean_wt = wt.replace(s_id, "").replace(" - YouTube", "").replace("YouTube", "").strip(" - ")
                                    clean_wt = clean_wt.replace("(snapshot)", "").strip()
                                    if len(clean_wt) > 5 and not self._is_title_generic(clean_wt):
                                        if verbose: logger_func(f"    🆘 [sos(wayback)] Encontrado (ytdl): {clean_wt}")
                                        return clean_wt, "sos(wayback)"
                        except: pass
            except: pass
            
        # 4. SOS Search (Google / DuckDuckGo)
        # Agresivamente silenciado
        for engine, query_pref in [("Google", "gvsearch1"), ("DuckDuckGo", "ddgsearch1")]:
            if verbose: logger_func(f"    ⏳ [sos({engine.lower()})] Buscando en {engine}...")
            try:
                # Prioridad 1: ID literal. Prioridad 2: Con contexto.
                for q in [s_id, f"youtube {s_id}"]:
                    with YoutubeDL(ydl_opts) as ydl:
                        res = ydl.extract_info(f"{query_pref}:{q}", download=False)
                        if res and "entries" in res and res["entries"]:
                            st = res["entries"][0].get("title")
                            if st:
                                clean_st = st.replace(s_id, "").replace(" - YouTube", "").strip(" - ")
                                # Filtro de calidad: debe tener longitud o contener el ID en el original
                                if (s_id in st or len(clean_st) > 5) and not self._is_title_generic(clean_st):
                                    if verbose: logger_func(f"    🆘 [sos({engine.lower()})] Encontrado: {clean_st}")
                                    return clean_st, f"sos({engine.lower()})"
            except: pass
            
        if verbose: logger_func(f"    ❌ No se pudo recuperar el título para {s_id}")
        return None, None

    def check_playlists(self, query=None, deep=False, only_recovered=False):
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

        should_save = True
        if deep:
            print("\n🚀 PCD (Deep Check): Escaneo agresivo SOS-Only.")
            print("Este modo ignora metadatos locales y busca directamente en archivos web y buscadores.")
            ans = input("¿Deseas guardar los resultados en el JSON al finalizar? (s/n): ").strip().lower()
            should_save = (ans == 's')
            if not should_save:
                print("ℹ️ Modo consulta: Los cambios se mostrarán pero NO se guardarán.")

        if deep:
            logging.info(f"🚀 INICIANDO COMPROBACIÓN PROFUNDA (SOS-Only) para {len(target_playlists)} playlist(s)...")
        else:
            logging.info(f"🔍 Verificando {len(target_playlists)} playlist(s) ({total_songs} canciones)...")
        
        ydl_opts_flat = _get_ytdl_opts(quiet=True)
        ydl_opts_flat.update({"extract_flat": True, "no_warnings": True, "quiet": True, "logger": None})
        ydl_opts_check = _get_ytdl_opts(quiet=True)
        ydl_opts_check.update({"no_warnings": True, "quiet": True, "logger": None})

        num_threads = self.config.get("pc_threads", 4)
        report_lock = threading.Lock()

        # Bucle principal
        with YoutubeDL(ydl_opts_check) as ydl:
            for pid, pdata in target_playlists.items():
                p_title = pdata.get("title", pid)
                
                # --- PASO 1: Intentar recuperar títulos vía metadatos de PLAYLIST ---
                meta_map = {}
                if len(pid) > 5 and not pid.startswith("migrated"):
                    try:
                        logging.info(f"⏳ Recuperando metadatos de la lista '{p_title}'...")
                        with YoutubeDL(ydl_opts_flat) as ydl_flat:
                            plist_info = ydl_flat.extract_info(f"https://www.youtube.com/playlist?list={pid}", download=False)
                            if plist_info and "entries" in plist_info:
                                for entry in plist_info["entries"]:
                                    if entry and entry.get("id"):
                                        t = entry.get("title")
                                        if t and not any(x in t.lower() for x in ["deleted video", "private video", "v\u00eddeo eliminado", "v\u00eddeo privado"]):
                                            meta_map[entry["id"]] = t
                    except: pass
                
                songs_list = pdata.get("songs", [])
                valid_indices = []
                
                def check_worker(index_song_tuple):
                    nonlocal recovered_any, total_deleted, processed_count
                    idx, s = index_song_tuple
                    s_id = s.get("id")
                    
                    # MEJORA: pcr (solo recuperadas)
                    if only_recovered and not s.get("recovery_method"):
                        return (idx, True, None) # (index, is_valid, report_line)

                    # Limpieza base
                    raw_title = s.get("title", s_id)
                    current_title = re.sub(r"[♻️🔎🆘\u267b\ufe0f]", "", raw_title).strip()
                    current_method = s.get("recovery_method")

                    # PASO 2: Disponibilidad Real
                    is_available = False
                    try:
                        ydl.extract_info(f"https://www.youtube.com/watch?v={s_id}", download=False)
                        is_available = True
                    except:
                        is_available = False

                    if is_available:
                        # Limpiar etiquetas si está vivo
                        updated = False
                        if current_method:
                            del s["recovery_method"]
                            updated = True
                        s["title"] = current_title
                        with report_lock:
                            processed_count += 1
                            print(f"👁️‍🗨️ [{processed_count}/{total_songs}] Disponible: {current_title} (en {p_title})")
                            if updated: recovered_any = True
                        return (idx, True, None)
                    
                    # PASO 3: Rescate (No disponible)
                    # Invalidar meta si no está en el mapa
                    if current_method == "meta" and s_id not in meta_map:
                        current_method = None
                    
                    is_generic = self._is_title_generic(current_title)
                    # MEJORA: Si es pcd/pcdr (deep), forzamos el intento de rescate ignorando etiquetas previas
                    if is_generic or current_title == s_id or not current_method or deep:
                        # Reintentos de rescate (max 3)
                        for attempt in range(1, 4):
                            recovered_title, source_label = self._rescue_id(s_id, meta_map=meta_map, verbose=(attempt==1), sos_only=deep)
                            if recovered_title:
                                s["title"] = recovered_title
                                s["recovery_method"] = source_label
                                if "recycled" in s: del s["recycled"]
                                current_title = recovered_title
                                current_method = source_label
                                with report_lock:
                                    recovered_any = True
                                    r_icon = "♻️" if source_label == "meta" else ("🔎" if source_label == "flat" else "🆘")
                                    logging.info(f"{r_icon} Título recuperado para {s_id} [{source_label}] (Intento {attempt}): {recovered_title}")
                                break
                            else:
                                if deep and attempt == 3:
                                    # Si es PCD y NO encontramos nada por SOS tras 3 intentos, marcamos como failed
                                    if not current_method or "sos" not in current_method:
                                        s["recovery_method"] = "failed"
                                        current_method = "failed"
                                        with report_lock: recovered_any = True

                    # Fallback failed (para PC normal o si pcd/pcdr falló tras retries)
                    if not s.get("recovery_method") and not self._is_title_generic(current_title) and current_title != s_id:
                        s["recovery_method"] = "failed"
                        current_method = "failed"
                        with report_lock: recovered_any = True

                    # Título final para el reporte
                    emo = "🆘" if (current_method == "failed" or (current_method and "sos" in current_method)) else ("♻️" if current_method == "meta" else ("🔎" if current_method == "flat" else ""))
                    
                    # MEJORA: Añadir procedencia detallada al reporte
                    source_tag = ""
                    if current_method == "failed":
                        source_tag = " failed"
                    elif current_method == "sos(wayback)":
                        source_tag = " (WayBack)"
                    elif current_method == "sos(google)":
                        source_tag = " (Google)"
                    elif current_method == "sos(ddg)":
                        source_tag = " (DDG)"
                    elif current_method == "meta":
                        source_tag = " (Meta)"
                    elif current_method == "flat":
                        source_tag = " (Flat)"

                    rep_title = f"{emo} {current_title} {emo}".strip() if emo else current_title
                    
                    with report_lock:
                        processed_count += 1
                        total_deleted += 1
                        print(f"❌ No disponible: {rep_title}{source_tag} ({s_id})")
                    
                    return (idx, False, f"- {rep_title}{source_tag} (ID: {s_id}) (Playlist: {p_title})")

                # Ejecutar hilos
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                    results = list(executor.map(check_worker, enumerate(songs_list)))
                
                # Procesar resultados
                for idx, is_valid, report_line in results:
                    if is_valid:
                        valid_indices.append(idx)
                    else:
                        if report_line:
                            unavailable_reports.append(report_line)
                
                if len(valid_indices) != len(songs_list):
                    all_indices = set(range(len(songs_list)))
                    invalid_indices = all_indices - set(valid_indices)
                    to_delete_map[pid] = sorted(list(invalid_indices), reverse=True)

        # Si recuperamos algo, guardamos los cambios en los títulos inmediatamente
        if recovered_any:
            if should_save:
                save_playlists(all_playlists)
                logging.info("\u2705 Se han actualizado los nombres recuperados en el archivo de playlists.")
            else:
                logging.info("📊 Resultados mostrados arriba (Cambios NO guardados en el JSON).")

        print("\n\u2705 Verificaci\u00f3n completada.")
        
        if total_deleted > 0:
            report_msg = "\n".join(unavailable_reports)
            print(f"\n\u26a0\ufe0f Se han detectado {total_deleted} canciones no disponibles:\n{report_msg}")
            
            # Solo preguntar por borrar si permitimos guardar cambios
            if not should_save:
                return "Escaneo completado en modo lectura."

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


    def ensure_id(self, s_id):
        print(f"\n🛠️ Diagnóstico de recuperación para ID: {s_id}")
        
        # 1. Local Meta (Búsqueda en playlists locales)
        all_p = load_playlists()
        local_meta_map = {}
        for pid, pdata in all_p.items():
            for s in pdata.get("songs", []):
                if s["id"] == s_id:
                    t = s.get("title")
                    if t and not self._is_title_generic(t):
                        # Limpiamos iconos si los tiene para el mapa de entrada
                        clean_t = re.sub(r"[♻️🔎🆘\u267b\ufe0f]", "", t).strip()
                        local_meta_map[s_id] = clean_t
                        break
            if s_id in local_meta_map: break
        
        # 2. Rescate Unificado con salida por print
        recovered_title, source_label = self._rescue_id(s_id, meta_map=local_meta_map, verbose=True, logger_func=print)
        
        if recovered_title:
            if source_label == "meta": r_icon = "♻️"
            elif source_label == "flat": r_icon = "🔎"
            else: r_icon = "🆘"
            print(f"\n✅ {r_icon} TÍTULO RECUPERADO [{source_label}]: {recovered_title}\n")
        else:
            print(f"\n❌ No se pudo recuperar el título para {s_id} tras agotar todos los métodos.\n")

        print("\n✅ Diagnóstico finalizado.")


    def _find_playlist(self, query):
        if not query: return None, None
        all_p = load_playlists()
        low_q = query.lower()
        if low_q in all_p: return low_q, all_p[low_q]
        for pid, data in all_p.items():
            if low_q in pid.lower() or low_q in data.get("title", "").lower():
                return pid, data
        return None, None


    def check_favorites(self):
        favs = load_favorites()
        if not favs: return "No hay favoritos para verificar"
        
        logging.info("🔍 Verificando favoritos...")
        deleted = []
        valid = []
        
        ydl_opts = _get_ytdl_opts(quiet=True)
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


    def _start_playlist_jit(self, songs, is_fav=False):
        if not songs: return
        info = get_search_info(songs[0]["id"])
        if info:
            # Fix: usar nombre de flag consistente con _start_playback
            if is_fav: info["_is_fav_playlist"] = True
            else: info["_is_plist"] = True
            
            fpath = download_media(info.get("url") or info.get("webpage_url"))
            if fpath:
                self._start_playback(info, fpath)
                with self._lock:
                    for s in songs[1:]: 
                        # Propagar flags para mantener contexto
                        meta = {"id": s["id"], "title": s["title"]}
                        if is_fav: meta["_is_fav_playlist"] = True
                        else: meta["_is_plist"] = True
                        self.queue.append((meta, None))
    def execute_command(self, cmd, args, voice_loop=None):
        """Hub central para procesar comandos de cualquier interfaz."""
        logging.info(f"DEBUG: Executing unified command: {cmd} {args}")

        if cmd == "help": print(AYUDA_MSG)
        elif cmd == "info": print(self.get_playback_info())
        elif cmd == "play": self.play_query(args["query"])
        
        elif cmd in ["pause", "resume", "toggle", "stop"]:
            if cmd == "stop": self.stop()
            elif cmd == "pause": self.pause()
            elif cmd == "resume": self.resume()
            else:
                if self._player and not self._paused: self.pause()
                else: self.resume()

        elif cmd == "next": self.next_result()
        elif cmd == "replay":
            if self._current_info and self._current_filepath:
                self._start_playback(self._current_info, self._current_filepath)

        elif cmd == "shuffle":
            with self._lock:
                if not self.queue: logging.info("⚠️ Cola vacía")
                else:
                    random.shuffle(self.queue)
                    self._preloaded_data = None
                    self._preloading = False
                    logging.info(f"🔀 Cola mezclada ({len(self.queue)})")

        elif cmd == "history":
            print("\n📜 ÚLTIMAS CANCIONES:")
            for i, t in enumerate(reversed(self.history)): print(f"  {i+1}. {t}")

        elif cmd in ["volume", "volume_rel", "mute", "unmute"]:
            if cmd == "volume": self.set_volume(args["n"])
            elif cmd == "mute": (setattr(self, '_saved_vol', self._volume), self.set_volume(0))
            elif cmd == "unmute": self.set_volume(int(getattr(self, '_saved_vol', 0.05) * 1000))
            else:
                change = 50 if args["direction"] == "up" else -50
                self.set_volume(max(0, min(200, int(self._volume * 1000) + change)))

        elif cmd in ["fav", "favlast", "favlist"]:
            if cmd == "favlist": print(f"\n⭐ MIS FAVORITOS:\n{self.get_favorites_text()}")
            else: logging.info(f"⭐️ {self.add_favorite(self._current_info if cmd=='fav' else self._previous_info)}")

        elif cmd == "add":
            query = args["query"]
            pid, pdata = self._find_playlist(query)
            if pdata:
                songs = pdata.get("songs", [])
                logging.info(f"📂 Encolando {len(songs)} canciones de '{pdata.get('title')}' (JIT)")
                with self._lock:
                    for s in songs: self.queue.append(({"id": s["id"], "title": s["title"]}, None))
                return

            logging.info(f"➕ Buscando para añadir: {query}")
            def _bg_add():
                info = None
                for i in range(10):
                    cand = get_search_info(query, index=i)
                    if cand and is_content_allowed(cand, self.config):
                        info = cand; break
                if info:
                    with self._lock:
                        self.queue.append((info, None))
                        if self.plist_mode and self.plist_id:
                            all_p = load_playlists()
                            if self.plist_id in all_p:
                                if not any(s["id"] == info["id"] for s in all_p[self.plist_id].get("songs", [])):
                                    all_p[self.plist_id].setdefault("songs", []).append({"id": info["id"], "title": info["title"]})
                                    save_playlists(all_p)
                    logging.info(f"✅ Añadido a la cola: {info['title']}")
                else: logging.warning(f"⚠️ No se encontró nada para: {query}")
            threading.Thread(target=_bg_add, daemon=True).start()

        elif cmd in ["playlist", "playlists", "playlist_remove", "import", "favcheck", "favrandom", "playfav", "playlistcheck", "playlistcheck_deep", "playlistcheck_recovered", "playlistcheck_deep_recovered"]:
            if cmd == "playlists":
                all_p = load_playlists()
                if not all_p: print("\n⚠️ No hay playlists.")
                else: 
                    print("\n📁 TUS PLAYLISTS:")
                    for pid, data in all_p.items(): print(f"  - [{pid}] {data.get('title')} ({len(data.get('songs',[]))} canciones)")
                return

            if cmd == "import": logging.info(f"📥 {self.import_playlist(args['url'])}")
            elif cmd == "favcheck": logging.info(f"🔍 {self.check_favorites()}")
            elif cmd in ["playlistcheck", "playlistcheck_deep", "playlistcheck_recovered", "playlistcheck_deep_recovered"]:
                deep = (cmd in ["playlistcheck_deep", "playlistcheck_deep_recovered"])
                recov = (cmd in ["playlistcheck_recovered", "playlistcheck_deep_recovered"])
                logging.info(f"🔍 {self.check_playlists(query=args.get('q'), deep=deep, only_recovered=recov)}")
            elif cmd == "favrandom" or cmd == "playfav":
                favs = load_favorites()
                if not favs: return print("⚠️ Lista vacía.")
                if cmd == "favrandom": random.shuffle(favs)
                self.plist_mode, self.plist_id, self.plist_title = True, "favs", "Favoritos" + (" (Aleatorio)" if cmd == "favrandom" else "")
                with self._lock: self.queue.clear()
                self._start_playlist_jit(favs, is_fav=True)
            
            else: # playlist o playlist_remove
                query = args.get("q")
                if not query and cmd == "playlist":
                    all_p = load_playlists()
                    for pid, data in all_p.items(): print(f"  - [{pid}] {data.get('title')} ({len(data.get('songs',[]))} canciones)")
                    return
                
                target_id, pdata = self._find_playlist(query)
                if not target_id: return print(f"⚠️ No se encontró '{query}'")
                
                if cmd == "playlist_remove":
                    if input(f"¿Eliminar '{pdata['title']}'? (s/n): ").lower() == 's':
                        all_p = load_playlists(); del all_p[target_id]; save_playlists(all_p); print("✅ Eliminada.")
                else:
                    self.plist_mode, self.plist_id, self.plist_title = True, target_id, pdata.get("title")
                    with self._lock: self.queue.clear()
                    self._start_playlist_jit(pdata.get("songs", []))

        elif cmd in ["radio", "filtros"]: getattr(self, f"toggle_{cmd}")(args["enabled"])
        elif cmd == "force":
            kw = self.set_forced_filter(args["f"])
            if kw: self.play_query(kw)
        elif cmd == "listen":
            self.config["listen_enabled"] = args["enabled"]
            save_config(self.config)
            logging.info(f"🎤 Micrófono {'activado' if args['enabled'] else 'desactivado'}")
        elif cmd == "exit": self.stop(); print("\n👋 ¡Hasta pronto!"); os._exit(0)
        elif cmd in ["set_mic", "list_mics"]:
            if cmd == "set_mic":
                idx = resolve_mic_index(args["n"])
                self.config["microphone_index"] = idx
                save_config(self.config)
            else: list_microphones()
        
        elif cmd == "ensure":
            self.ensure_id(args["id"])

# -----------------------------------------------------------
# Bucle de voz
# -----------------------------------------------------------
class VoiceLoop:
    def __init__(self, player: AudioPlayer, hotword: str | None = None):
        self.player = player
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
        self.player.execute_command(cmd, args, voice_loop=self)


def cli_loop(player: AudioPlayer, hotword: str | None = None, voice_loop: 'VoiceLoop' = None):
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
            
            player.execute_command(cmd, args, voice_loop=voice_loop)

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
                    try:
                        def parse_v(v): return tuple(map(int, v.split(".")))
                        if parse_v(remote_version) > parse_v(VTM_VERSION):
                            print("\n" + "!" * 60)
                            print(f"🚀 ¡NUEVA VERSIÓN DISPONIBLE EN GITHUB! ({remote_version})")
                            print(f"   Versión instalada: {VTM_VERSION}")
                            print(f"   Link: https://github.com/Cicker21/VTM")
                            print("!" * 60 + "\n")
                            time.sleep(2)
                        else:
                            print(f"\n✅ Tienes la última versión ({VTM_VERSION}).\n")
                    except Exception as e:
                        logging.warning(f"⚠️ No se pudo comprobar actualizaciones: {e}")
                        pass 
                else:
                    print(f"\n✅ Tienes la última versión ({VTM_VERSION}).\n")
            else:
                print(f"\n✅ Tienes la última versión ({VTM_VERSION}).\n")
    except Exception as e:
        logging.warning(f"⚠️ No se pudo comprobar actualizaciones: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--texto", action="store_true")
    ap.add_argument("--radio-init", choices=["on", "off"], default="on")
    ap.add_argument("--micro-init", choices=["on", "off"], default=None)
    ap.add_argument("--listar-micros", action="store_true")
    args = ap.parse_args()

    if args.listar_micros:
        list_microphones()
        return



    cleanup_temp_files(prefix=TEMP_AUDIO_PREFIX)
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
    v_loop = VoiceLoop(player, hotwords)
    threading.Thread(target=v_loop.run, daemon=True).start()

    # --- Main Thread: CLI Loop (Always active now) ---
    cli_loop(player, hotwords[0] if hotwords else None, voice_loop=v_loop)

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
