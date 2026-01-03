import os
import pickle
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
from google.auth.transport.requests import Request

# --- CONFIGURACIÓN ---
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.pickle")
PLAYLIST_ID = "PLDPMSuQq9IWFS-cQG45mJmpgBWGRrOWnJ"

def get_authenticated_service():
    """Maneja el flujo de OAuth2 y devuelve el objeto de servicio de YouTube."""
    credentials = None
    
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            credentials = pickle.load(token)
            
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    f"❌ Error: No se encontró 'client_secrets.json'.\n"
                    f"   Descárgalo desde Google Cloud Console y colócalo en: {SCRIPT_DIR}"
                )
                
            flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, SCOPES)
            credentials = flow.run_local_server(port=0)
            
        with open(TOKEN_FILE, "wb") as token:
            pickle.dump(credentials, token)

    return googleapiclient.discovery.build("youtube", "v3", credentials=credentials)

def purge_playlist():
    try:
        print("🔐 Autenticando con OAuth 2.0...")
        youtube = get_authenticated_service()
        print("✅ Autenticación exitosa.\n")
    except Exception as e:
        print(f"❌ Error de autenticación: {e}")
        return

    print(f"🔍 Obteniendo todos los videos de la playlist {PLAYLIST_ID}...\n")
    
    videos = []
    next_page_token = None
    
    while True:
        try:
            request = youtube.playlistItems().list(
                part="snippet,contentDetails,status",
                playlistId=PLAYLIST_ID,
                maxResults=50,
                pageToken=next_page_token
            )
            response = request.execute()
            
            videos.extend(response.get("items", []))
            next_page_token = response.get("nextPageToken")
            if not next_page_token: 
                break
        except Exception as e:
            print(f"❌ Error obteniendo videos: {e}")
            break

    print(f"✅ Se han recuperado {len(videos)} entradas en total.\n")
    
    ghosts = []
    for item in videos:
        v_id = item["contentDetails"]["videoId"]
        item_id = item["id"]
        title = item["snippet"]["title"]
        privacy = item["status"].get("privacyStatus")
        
        is_ghost = False
        if any(w in title for w in ["Deleted video", "Private video", "Video eliminado", "Video privado"]):
            is_ghost = True
        # Considerar privados, eliminados y NO LISTADOS como problemáticos
        if privacy in ["private", "deleted", "unlisted"]:
            is_ghost = True
            
        status_label = "GHOST" if is_ghost else "OK"
        print(f"[{status_label}] ID: {v_id} | Title: {title} | Status: {privacy}")
        
        if is_ghost:
            ghosts.append({"item_id": item_id, "title": title, "v_id": v_id})

    if not ghosts:
        print("\n✨ No se detectaron videos problemáticos. ¡Tu playlist está limpia!")
        return

    print(f"\n🚨 Se han detectado {len(ghosts)} videos problemáticos (eliminados/privados/no listados):")
    print("\n" + "="*80)
    for i, g in enumerate(ghosts, 1):
        print(f"{i:2d}. [{g['v_id']}] {g['title']}")
    print("="*80)

    print(f"\n⚠️  Esto eliminará {len(ghosts)} videos de tu playlist de forma PERMANENTE.")
    confirm = input("¿Deseas continuar con el borrado? Escribe 'SI' para confirmar: ").strip()
    
    if confirm.upper() == 'SI':
        print("\n🗑️  Iniciando borrado...\n")
        deleted_count = 0
        for g in ghosts:
            print(f"   Eliminando: {g['title'][:60]}...", end=" ")
            try:
                youtube.playlistItems().delete(id=g["item_id"]).execute()
                print("✅")
                deleted_count += 1
            except googleapiclient.errors.HttpError as e:
                print(f"❌ Error: {e}")
        
        print(f"\n🎉 Proceso completado. {deleted_count}/{len(ghosts)} videos eliminados.")
    else:
        print("\n❌ Operación cancelada. No se eliminó nada.")

    # Limpieza de seguridad
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            print("\n🔒 Token de seguridad eliminado automáticamente.")
        except:
            pass

if __name__ == "__main__":
    purge_playlist()
