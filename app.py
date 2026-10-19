import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
import streamlit.components.v1 as components
from google import genai
from google.genai import types

st.set_page_config(page_title="Descargador de Fotos y Ficha Next", page_icon="📥", layout="wide")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

ASESORES = {
    "Cristian Sosa": {
        "nombre": "Cristian Sosa"
    },
    "Claudia Castro": {
        "nombre": "Claudia Castro"
    }
}

# ==========================================
# EXTRACCIÓN Y SCRAPING MEJORADO
# ==========================================

def extraer_datos_inmueble(url):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    resp = session.get(url, headers=headers, timeout=20)
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'html.parser')

    # Título principal
    og_title = ""
    meta_t = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if meta_t and meta_t.get("content"):
        og_title = meta_t["content"].strip()
    if not og_title:
        h1 = soup.find("h1")
        og_title = h1.get_text(strip=True) if h1 else ""

    og_desc = ""
    meta_d = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    if meta_d and meta_d.get("content"):
        og_desc = meta_d["content"].strip()

    # Extraer texto de la propiedad preservando detalles
    # Eliminar únicamente scripts, estilos y menús de navegación
    for s in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        s.extract()

    # Buscar contenedor específico de la descripción si existe
    contenedor_desc = (
        soup.find(class_=re.compile(r'(property-description|descripcion|listing-detail|contenido-propiedad)', re.I)) or
        soup.find(id=re.compile(r'(property-description|descripcion|listing-detail)', re.I))
    )
    
    if contenedor_desc:
        texto_principal = contenedor_desc.get_text(separator="\n", strip=True)
    else:
        texto_principal = soup.get_text(separator="\n", strip=True)

    # Filtrar imágenes de alta resolución
    palabras_prohibidas = ["asesor", "agente", "broker", "logo", "avatar", "icon", "user", "staff"]
    imagenes = []

    meta_img = soup.find("meta", property="og:image")
    if meta_img and meta_img.get("content"):
        img_url = meta_img["content"]
        if "http" in img_url and not any(p in img_url.lower() for p in palabras_prohibidas):
            imagenes.append(img_url)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").lower()
        if not src or any(p in src.lower() for p in palabras_prohibidas) or any(p in alt for p in palabras_prohibidas):
            continue
        if any(term in src.lower() for term in ["uploads", "listings", "propiedades", "images"]):
            full_src = re.sub(r'-[0-9]+x[0-9]+', '', src)
            if not full_src.startswith("http"):
                full_src = f"https://nextbr.mx{full_src}"
            if full_src not in imagenes:
                imagenes.append(full_src)

    matches_raw = re.findall(r'https?://[^\s"\']+\.(?:jpe?g|png|webp)', resp.text, re.IGNORECASE)
    for m in matches_raw:
        m_lower = m.lower()
        if ("listings" in m_lower or "uploads" in m_lower) and not any(p in m_lower for p in palabras_prohibidas):
            clean_m = re.sub(r'-[0-9]+x[0-9]+', '', m)
            if clean_m not in imagenes:
                imagenes.append(clean_m)

    return og_title, og_desc, texto_principal, imagenes

# ==========================================
# ESTRUCTURACIÓN CON IA (SIN TELÉFONO / SIN ALUCINACIONES)
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre):
    client = genai.Client(api_key=API_KEY)
    prompt = f"""
    Eres un asesor inmobiliario profesional especializado en redactar anuncios para Facebook Marketplace.
    Tu trabajo es extraer TODA la información real del inmueble y organizarla de forma atractiva, limpia y fácil de leer.

    DATOS DE ENTRADA:
    URL: {url}
    Título: {og_title}
    Resumen: {og_desc}
    Texto completo del inmueble:
    {body_text[:7000]}

    Asesor responsable: {asesor_nombre}

    REGLAS ESTRICTAS DE REDACCIÓN:
    1. CERO ALUCINACIONES: Incluye absolutamente TODOS los detalles mencionados en el texto (por ejemplo: tipos de luz como 110/trifásica, terrazas con m², estacionamientos, bodegas, lavaplatos, si es más IVA o facturación, ubicación sobre vialidades principales o zonas de alta afluencia, etc.). No inventes nada que no esté en el texto ni omitas detalles existentes.
    2. PRIMERA LÍNEA OBLIGATORIA: La primera línea debe ser exactamente:
       "[tipo_inmueble] en [operacion] en colonia [colonia]"
       Todo en minúsculas (ejemplo: "local comercial en renta en colonia constitucion" o "casa en venta en colonia montecarlo").
    3. PRECIO: Pon el precio exacto sin centavos (ej: "$18,000 MXN"). Si menciona "más IVA" o "precio más IVA", agrégalo claramente junto al precio.
    4. SIN TELÉFONO NI ENLACES DE WHATSAPP: Facebook Marketplace sanciona o bloquea publicaciones con números de teléfono o links de WhatsApp. NO pongas números de teléfono, ni enlaces wa.me, ni correos.
    5. CIERRE DE CONTACTO: Cierra la publicación con:
       📩 *Para más información, citas o disponibilidad, envía un mensaje directo.*
       *{asesor_nombre}* - Next Bienes Raíces

    6. ESTRUCTURA REQUERIDA PARA 'copy_publicitario':
       [tipo_inmueble] en [operacion] en colonia [colonia]

       💰 *Precio:* [PRECIO] [MONEDA] [aclaración de IVA si aplica]

       📍 *Ubicación:*
       (Detalles de vialidad, zona de plusvalía o afluencia si se mencionan)

       📐 *Medidas y Distribución:*
       • Superficie de terreno / construcción (si se indican)
       • Estacionamientos
       • Baños
       • Espacios especiales (terraza, bodega, etc.)

       ⚡ *Servicios e Instalaciones:*
       • Tipo de luz / instalaciones eléctricas (ej: 110 y trifásica)
       • Equipamiento adicional (lavaplatos, aljibe, etc.)

       📝 *Condiciones y Notas:*
       • Facturación, tipo de comercio ideal o requerimientos señalados

       📩 *Para más información y citas, envía un mensaje directo.*
       *{asesor_nombre}* - Next Bienes Raíces

    Devuelve un JSON estrictamente válido con este formato:
    {{
      "colonia": "Constitución",
      "tipo_inmueble": "local comercial",
      "operacion": "renta",
      "precio": "$18,000",
      "moneda": "MXN",
      "copy_publicitario": "..."
    }}
    """

    modelos = ['gemini-2.5-flash', 'gemini-1.5-flash', 'gemini-2.5-pro']
    for mod in modelos:
        try:
            res = client.models.generate_content(
                model=mod,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(res.text)
            # Limpieza de seguridad adicional para que jamás aparezcan teléfonos ni links
            if "copy_publicitario" in data:
                copy = data["copy_publicitario"]
                copy = re.sub(r'https?://[^\s]+', '', copy)
                copy = re.sub(r'\bwa\.me[^\s]*', '', copy)
                copy = re.sub(r'\b\d{10}\b', '', copy)
                data["copy_publicitario"] = copy.strip()
            return data
        except Exception:
            time.sleep(1.5)
            continue

    raise Exception("No fue posible procesar la ficha con el modelo. Revisa la clave API o intenta de nuevo.")

# ==========================================
# INICIALIZACIÓN DE ESTADO PERSISTENTE
# ==========================================

if "datos_extraidos" not in st.session_state:
    st.session_state.datos_extraidos = None
if "fotos_extraidas" not in st.session_state:
    st.session_state.fotos_extraidas = []
if "descargar_ahora" not in st.session_state:
    st.session_state.descargar_ahora = False

# ==========================================
# INTERFAZ STREAMLIT
# ==========================================

st.title("📥 Descargador de Fotos y Ficha para Marketplace")
st.write("Extrae todas las fotos en alta resolución y genera el copy completo sin datos de contacto que puedan penalizar la publicación:")

col_url, col_as = st.columns([2, 1])
with col_url:
    url_input = st.text_input("Enlace de la propiedad:", placeholder="https://nextbr.mx/propiedades/...")
with col_as:
    asesor_seleccionado = st.selectbox("Selecciona Asesor:", options=list(ASESORES.keys()), index=0)

info_asesor = ASESORES[asesor_seleccionado]

if st.button("🔍 Extraer Fotos y Generar Ficha", type="primary"):
    if not url_input.strip().startswith("http"):
        st.error("Por favor ingresa un enlace válido.")
    elif not API_KEY:
        st.error("Falta configurar GEMINI_API_KEY en Secrets.")
    else:
        with st.spinner("Analizando portal y extrayendo todos los datos del inmueble..."):
            try:
                og_t, og_d, b_txt, fotos = extraer_datos_inmueble(url_input.strip())
                datos = procesar_con_ia(url_input.strip(), og_t, og_d, b_txt, info_asesor['nombre'])
                
                st.session_state.datos_extraidos = datos
                st.session_state.fotos_extraidas = fotos
                st.session_state.descargar_ahora = False
                st.success(f"¡Extracción exitosa! Se encontraron {len(fotos)} fotografías.")
            except Exception as e:
                st.error(f"Error al analizar el inmueble: {e}")

# ==========================================
# RESULTADOS
# ==========================================
if st.session_state.datos_extraidos and st.session_state.fotos_extraidas:
    st.markdown("---")
    datos = st.session_state.datos_extraidos
    fotos = st.session_state.fotos_extraidas

    col_descargas, col_copy = st.columns([1, 1])

    with col_descargas:
        st.subheader(f"🖼️ Fotografías Encontradas ({len(fotos)})")
        
        # Botón para descargar todas en 1 clic
        if st.button("🚀 Descargar Todas las Fotos en 1 Clic (Individuales)", type="primary", use_container_width=True):
            st.session_state.descargar_ahora = True

        if st.session_state.descargar_ahora:
            json_fotos = json.dumps(fotos)
            js_script = f"""
            <script>
            const urls = {json_fotos};
            async function descargarTodas() {{
                for (let i = 0; i < urls.length; i++) {{
                    try {{
                        const resp = await fetch(urls[i]);
                        const blob = await resp.blob();
                        const a = document.createElement('a');
                        a.href = URL.createObjectURL(blob);
                        a.download = `Foto_${{i + 1}}.jpg`;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        await new Promise(r => setTimeout(r, 250));
                    }} catch (e) {{
                        const a = document.createElement('a');
                        a.href = urls[i];
                        a.target = '_blank';
                        a.download = `Foto_${{i + 1}}.jpg`;
                        a.click();
                    }}
                }}
            }}
            descargarTodas();
            </script>
            <div style="padding: 10px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; color: #166534; font-size: 14px;">
                ✅ <b>Iniciando descarga individual de {len(fotos)} fotos...</b><br>
                <i>Nota: Si tu navegador pide permiso para "descargar varios archivos", haz clic en Permitir.</i>
            </div>
            """
            components.html(js_script, height=90)

        st.markdown("##### O descarga manual por enlace directo:")
        with st.expander("Ver lista de enlaces individuales", expanded=False):
            for i, f_url in enumerate(fotos):
                st.markdown(f"- [Descargar Foto #{i + 1}]({f_url})")

    with col_copy:
        st.subheader("📋 Descripción para Marketplace")
        st.caption("Copia la descripción con el botón en la esquina superior derecha del recuadro:")
        st.code(datos.get("copy_publicitario", ""), language="markdown")
