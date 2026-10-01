import os
import sys
import time
import io
from PIL import Image

try:
    from instagram_poster import _publicar_en_instagram, _subir_a_imgbb, _env_limpio
except ImportError as e:
    print(f"Error importando instagram_poster: {e}")
    sys.exit(1)

def main():
    print("=== PRUEBA DE PUBLICACION EN INSTAGRAM (VIA CATBOX) ===")
    
    # Intentar obtener las variables de entorno tal como lo hace el bot
    access_token = _env_limpio("IG_ACCESS_TOKEN")
    user_id = _env_limpio("IG_USER_ID")
    
    if not access_token:
        access_token = input("No se encontro IG_ACCESS_TOKEN en el sistema. Pegalo aqui: ").strip()
    if not user_id:
        user_id = input("No se encontro IG_USER_ID en el sistema. Pegalo aqui: ").strip()
        
    if not access_token or not user_id:
        print("Faltan credenciales. Cancelando prueba.")
        sys.exit(1)

    print("\n1. Creando imagen de prueba (100x100 rojo)...")
    img = Image.new('RGB', (100, 100), color='red')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    img_bytes = buf.getvalue()

    print("2. Subiendo a Catbox.moe...")
    try:
        start = time.time()
        # Usamos _subir_a_imgbb que por dentro ya usa Catbox
        image_url = _subir_a_imgbb(img_bytes, "fake_key")
        print(f"✅ Exito! Imagen subida en {time.time() - start:.2f}s.")
        print(f"   URL: {image_url}")
    except Exception as e:
        print(f"❌ Error subiendo a Catbox: {e}")
        sys.exit(1)

    print("\n3. Publicando en Instagram Graph API...")
    try:
        start = time.time()
        caption = "Prueba de conexión exitosa usando Catbox como puente de imágenes."
        post_id = _publicar_en_instagram(user_id, access_token, image_url, caption)
        print(f"✅ Exito! Publicado en Instagram en {time.time() - start:.2f}s.")
        print(f"   Media ID: {post_id}")
        print("\n🎉 Revisa tu perfil de Instagram, ¡la imagen debería estar ahí!")
    except Exception as e:
        print(f"❌ Error publicando en Instagram: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
