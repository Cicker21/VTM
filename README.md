# VTM - Voice To Music

Reproductor de música de YouTube para Windows controlado por voz y texto.

## 🚀 Requisitos e Instalación

### 1. Requisitos de Sistema
Es necesario tener instalado **FFmpeg** en el sistema y añadido a las variables de entorno (PATH). Puedes descargarlo desde [ffmpeg.org](https://ffmpeg.org/).

### 2. Instalación de Dependencias
Asegúrate de tener Python instalado y ejecuta el siguiente comando para instalar las librerías necesarias:

```bash
pip install yt-dlp ffpyplayer SpeechRecognition PyAudio
```

> **Nota:** Si tienes problemas instalando `PyAudio` en Windows, puedes usar [estos wheels](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio) o intentar con `pip install pipwin` y luego `pipwin install pyaudio`.

## 📂 Uso
Para iniciar la aplicación, ejecuta el archivo principal:

```bash
python vtm.py
```

### Argumentos de línea de comandos destacados:
- `--texto`: Inicia solo en modo texto (sin escucha automática).
- `--navegador`: Abre los vídeos en el navegador además de reproducirlos.
- `--radio-init [on/off]`: Activa o desactiva el modo radio al inicio.

---

## 📋 Lista de Comandos

### 🎵 REPRODUCCIÓN
- **p / pon [q]**: Reproducir canción o búsqueda.
- **p**: Pausa / Reanudar (Toggle).
- **s / n / siguiente**: Siguiente canción.
- **stop / detener**: Para la música.
- **replay / otra vez**: Reinicia el tema actual.
- **ap / historial**: Canciones que ya han sonado.
- **r / shuffle / aleat**: Mezclara la cola actual.
- **add / a [q]**: Añadir a la cola sin interrumpir.

### 🔊 AUDIO Y CONTROL
- **+ / - / v [n]**: Subir/Bajar volumen o fijar [0-200].
- **m / silencio**: Silenciar (Toggle).
- **micro [on/off]**: Activar/Desactivar micrófono.
- **micros / miclist**: Listar micrófonos disponibles.
- **micro [n]**: Cambiar a micrófono índice [n].

### 📂 PLAYLISTS
- **ps / playlists**: Ver todas tus listas importadas.
- **import [url]**: Importar lista de YouTube.
- **pp [nombre]**: Reproducir una de tus listas.
- **pr [nombre]**: Eliminar una playlist.
- **pc / pc2 [q]**: Verificar links (pc2 = modo profundo).

### ⭐️ FAVORITOS
- **fav / me gusta**: Guardar actual en favoritos.
- **favlast**: Guardar la anterior en favoritos.
- **fp / playfav**: Reproducir tus favoritos.
- **rf / favrandom**: Modo aleatorio de favoritos.
- **favlist**: Listar todos tus favoritos.
- **favcheck**: Verificar disponibilidad de favoritos.

### ⚙️ AJUSTES Y SISTEMA
- **info / estado**: ¿Qué está sonando?
- **radio [on/off]**: Modo radio al vaciarse la cola.
- **con/sin filtros**: Activar/Quitar filtros de YouTube.
- **forzar [palabra]**: Filtrar radio por una palabra clave.
- **modo [directo/nav]**: Cambia motor de descarga (Directo = ffpyplayer, Nav = Abrir pestaña).
- **h / ayuda / help**: Mostrar la lista de comandos.
- **salir / terminar**: Cerrar la aplicación.

---
*Desarrollado para facilitar el acceso a la música mediante comandos intuitivos.*
