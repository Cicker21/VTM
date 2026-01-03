# VTM - Voice To Music

**VTM** es un ecosistema de herramientas diseñado para disfrutar de música de YouTube consumiendo los mínimos recursos posibles. Su enfoque principal es la gestión robusta de playlists, la recuperación de contenido perdido ("ghosts") y la compatibilidad con control por voz.

El proyecto se divide en 3 componentes paralelos:

1.  **VTM Desktop** (👑 Principal)
2.  **VTM Discord** (Servidor)
3.  **VTM Purger** (Mantenimiento)

---

## 1. VTM Desktop (Principal)
El núcleo del proyecto. Un reproductor de escritorio ultraligero capaz de manejar bibliotecas musicales masivas sin el consumo de RAM de un navegador web.

**Características:**
*   Reproducción de bajo consumo (Audio Only).
*   Gestión avanzada de PLaylists locales.
*   **Modo SOS**: Recuperación automática de canciones borradas mediante WayBack Machine y buscadores alternativos.
*   Control híbrido: Texto (CLI) y Voz.

### 📋 To-Do Desktop
- [ ] Optimizar el consumo de recursos.
- [ ] Verificar que no queda código muerto ni redundante.
- [ ] Estabilidad en general, evitar que el bot muera silenciosamente.
- [ ] Verificar que ayuda contempla todos los regex.
- [ ] Verificar que no hay comandos que hayan muerto al recodificar funciones.

---

## 2. VTM Discord (Bot)
Un bot de música personal que replica la experiencia de VTM Desktop en servidores de Discord. Ideal para sesiones compartidas manteniendo la lógica de bajo consumo y cero anuncios.

### � To-Do Discord
- [ ] Mejorar la robustez de los comandos por voz.
- [ ] Implementar funcinalidades de VTM Desktop.
- [ ] Estabilidad en general.
- [ ] Control de versiones.

---

## 3. VTM Purger (Mantenimiento)
Herramienta especializada en la limpieza y saneamiento de playlists de YouTube.

**Función:**
Detecta y elimina videos "Fantasmas" (Deleted/Private/Unlisted) que ensucian las listas de reproducción y causan errores en otros reproductores. Utiliza la API Oficial de YouTube para garantizar una visión sin filtros de la realidad de la playlist.

### 📋 To-Do Purger
- [ ] Crear algún tipo de tutorial para secrets.json y OAUTH 2.0.
- [X] Verificar que funcione correctamente.

---

## 🚀 Instalación y Uso (General)

### Requisitos
*   Python 3.10+
# Pip
*   speech_recognition
*   yt-dlp
*   ffpyplayer

### Comandos Comunes (Desktop)
*   `p [canción]`: Reproducir.
*   `s`: Siguiente.
*   `import [url]`: Importar playlist.
*   `pc`: Chequeo de integridad de playlists.


