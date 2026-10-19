import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
import io
import zipfile
from google import genai
from google.genai import types

st.set_page_config(page_title="Descargador de Fotos y Ficha Next", page_icon="📥", layout="wide")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", "")).strip()

ASESORES = {
    "Cristian Sosa": {
        "nombre": "Cristian Sosa"
    },
    "Claudia Castro": {
        "nombre": "Claudia Castro"
    }
}

# ==========================================
# UTILIDAD DE CAPITALIZACIÓN FORMAL
# ==========================================

def normalizar_oracion(texto):
    t = texto.strip()
    if not t:
        return ""
    if t.isupper() or sum(1 for c in t if c.isupper()) > len(t) * 0.6:
        t = t.lower()
        t = t[0].upper() + t[1:]
    reemplazos = {
        r'\bmorelos\b': 'Morelos',
        r'\bconstitucion\b': 'Constitución',
        r'\bhermosillo\b': 'Hermosillo',
        r'\bsonora\b': 'Sonora',
        r'\bmexico\b': 'México',
        r'\bblvd\.?\b': 'Blvd.',
        r'\biva\b': 'IVA',
        r'\bm2\b': 'm²'
    }
    for patron, repl in reemplazos.items():
        t = re.sub(patron, repl, t, flags=re.IGNORECASE)
    return t

# ==========================================
# EXTRACCIÓN Y SCRAPING
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

    for bloque in soup.find_all(lambda t: any(cl in str(t.get('class', '')).lower() or cl in str(t.get('id', '')).lower() 
                                            for cl in ['perfil-asesor', 'agent-box', 'agent-info', 'author-box'])):
        bloque.extract()

    for s in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        s.extract()

    contenedor_desc = (
        soup.find(class_=re.compile(r'(property-description|descripcion|listing-detail|contenido-propiedad)', re.I)) or
        soup.find(id=re.compile(r'(property-description|descripcion|listing-detail)', re.I))
    )
    
    if contenedor_desc:
        texto_principal = contenedor_desc.get_text(separator="\n", strip=True)
    else:
        texto_principal = soup.get_text(separator="\n", strip=True)

    patrones_asesor = [
        "avatar", "logo", "icon", "perfil-", "asesor", "/agent/", "/agents/", 
        "cristian-sosa", "claudia-castro", "team-", "/staff/", "favicon"
    ]

    imagenes = []

    meta_img = soup.find("meta", property="og:image")
    if meta_img and meta_img.get("content"):
        img_url = meta_img["content"]
        if "http" in img_url and not any(p in img_url.lower() for p in patrones_asesor):
            imagenes.append(img_url)

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        alt = (img.get("alt") or "").lower()
        src_lower = src.lower()
        
        if not src or any(p in src_lower for p in patrones_asesor) or any(p in alt for p in patrones_asesor):
            continue
        
        if any(term in src_lower for term in ["uploads", "listings", "propiedades", "images"]):
            full_src = re.sub(r'-[0-9]+x[0-9]+', '', src)
            if not full_src.startswith("http"):
                full_src = f"https://nextbr.mx{full_src}"
            if full_src not in imagenes:
                imagenes.append(full_src)

    matches_raw = re.findall(r'https?://[^\s"\']+\.(?:jpe?g|png|webp)', resp.text, re.IGNORECASE)
    for m in matches_raw:
        m_lower = m.lower()
        if any(term in m_lower for term in ["uploads", "listings", "propiedades"]) and not any(p in m_lower for p in patrones_asesor):
            clean_m = re.sub(r'-[0-9]+x[0-9]+', '', m)
            if clean_m not in imagenes:
                imagenes.append(clean_m)

    return og_title, og_desc, texto_principal, imagenes

# ==========================================
# PARSEADOR LOCAL DE RESPALDO
# ==========================================

def armar_ficha_local(og_title, og_desc, body_text, asesor_nombre):
    texto_combinado = f"{og_title}\n{og_desc}\n{body_text}"
    texto_lower = texto_combinado.lower()

    tipo = "Local comercial"
    for t_raw, t_nom in [("local comercial", "Local comercial"), ("departamento", "Departamento"), 
                         ("oficina", "Oficina"), ("casa", "Casa"), ("bodega", "Bodega"), ("terreno", "Terreno")]:
        if t_raw in texto_lower:
            tipo = t_nom
            break

    operacion = "renta" if "renta" in texto_lower else "venta"

    colonia = "Constitución"
    col_match = re.search(r'(?:colonia|col\.?)\s+([A-Za-zÁÉÍÓÚñáéíóú\s]+?)(?:\n|,|\.)', texto_combinado, re.I)
    if col_match:
        colonia = col_match.group(1).strip().title()

    p_match = re.search(r'\$\s*([0-9]{1,3}(?:,[0-9]{3})+)', texto_combinado)
    precio = f"${p_match.group(1)}" if p_match else ""

    lineas = [l.strip() for l in body_text.split("\n") if len(l.strip()) > 3]
    caracteristicas = []
    descartar = ["next", "inmobiliaria", "contacto", "teléfono", "whatsapp", "descripción", "derechos", "aviso", "buscar", "precio"]
    
    for l in lineas:
        l_low = l.lower()
        if not any(d in l_low for d in descartar) and not l.startswith("$") and len(l) < 140:
            l_limpia = normalizar_oracion(l)
            if l_limpia not in caracteristicas and not l_limpia.lower().startswith("local comercial en renta"):
                caracteristicas.append(l_limpia)

    copy = f"{tipo} en {operacion} en colonia {colonia}\n\n"
    if precio:
        copy += f"💰 *Precio:* {precio} MXN"
        if "iva" in texto_lower:
            copy += " (más IVA, se factura)"
        copy += "\n\n"

    copy += "✨ *Características y Distribución:*\n"
    for c in caracteristicas:
        copy += f"• {c}\n"

    copy += f"\n📩 *Para más información, citas o disponibilidad, envía un mensaje directo.*\n*{asesor_nombre}* - Next Bienes Raíces"

    return {
        "colonia": colonia,
        "tipo_inmueble": tipo,
        "operacion": operacion,
        "precio": precio,
        "copy_publicitario": copy
    }

# ==========================================
# ESTRUCTURACIÓN CON IA
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre):
    if not API_KEY:
        return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

    client = genai.Client(api_key=API_KEY)
    prompt = f"""
    Eres un asesor inmobiliario profesional de élite. Redacta una ficha publicitaria formal para Facebook Marketplace.

    DATOS DE LA PROPIEDAD:
    URL: {url}
    Título: {og_title}
    Descripción: {og_desc}
    Texto completo:
    {body_text[:6000]}

    Asesor: {asesor_nombre}

    REGLAS ESTRICTAS DE REDACCIÓN Y ORTOGRAFÍA:
    1. PROHIBIDAS LAS MAYÚSCULAS SOSTENIDAS: Nunca escribas frases completas en mayúsculas como "ALTA AFLUENCIA VEHICULAR" o "LUZ 110 Y TRIFASICA". Usa mayúscula únicamente en la primera letra de la oración y en nombres propios (ej: "Alta afluencia vehicular", "Luz 110 y trifásica", "Sobre Blvd. Morelos").
    2. PRIMERA LÍNEA OBLIGATORIA:
       "[Tipo de inmueble] en [operación] en colonia [Nombre de la Colonia]"
       (Ejemplo exacto: "Local comercial en renta en colonia Constitución" o "Casa en venta en colonia Jardín Real"). Primera letra con mayúscula y nombre de colonia como nombre propio.
    3. CERO ALUCINACIONES: Incluye ABSOLUTAMENTE TODOS los puntos del anuncio (luz 110 y trifásica, m² de terraza, estacionamientos, bodega con lavaplatos, si es más IVA o requiere factura, plusvalía, etc.).
    4. SIN TELÉFONO NI ENLACES DE WHATSAPP.
    5. CIERRE FORMAL:
       📩 *Para más información, citas o disponibilidad, envía un mensaje directo.*
       *{asesor_nombre}* - Next Bienes Raíces

    ESTRUCTURA EXACTA DE 'copy_publicitario':
    [Tipo de inmueble] en [operación] en colonia [Nombre de la Colonia]

    💰 *Precio:* [Precio] [Moneda] [Aclaración de IVA si aplica]

    📍 *Ubicación y Entorno:*
    (Mencionar vialidades principales, afluencia y plusvalía)

    📐 *Medidas y Distribución:*
    • Superficie de terreno / construcción (si se indican)
    • Estacionamientos
    • Baños
    • Espacios especiales (terraza, bodega, etc.)

    ⚡ *Instalaciones y Equipamiento:*
    • Detalles de luz y suministros

    📝 *Condiciones y Notas:*
    • Facturación y detalles adicionales

    📩 *Para más información, citas o disponibilidad, envía un mensaje directo.*
    *{asesor_nombre}* - Next Bienes Raíces

    Devuelve un JSON estrictamente válido:
    {{
      "colonia": "Constitución",
      "tipo_inmueble": "Local comercial",
      "operacion": "renta",
      "precio": "$18,000",
      "moneda": "MXN",
      "copy_publicitario": "..."
    }}
    """

    modelos = ['gemini-2.5-flash', 'gemini-1.5-flash']
    for mod in modelos:
        try:
            res = client.models.generate_content(
                model=mod,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(res.text)
            if "copy_publicitario" in data:
                copy = data["copy_publicitario"]
                copy = re.sub(r'https?://[^\s]+', '', copy)
                copy = re.sub(r'\bwa\.me[^\s]*', '', copy)
                copy = re.sub(r'\b\d{10}\b', '', copy)
                data["copy_publicitario"] = copy.strip()
            return data
        except Exception:
            time.sleep(1)
            continue

    return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

# ==========================================
# GESTIÓN DE DESCARGAS (MEMORIA BUFFER)
# ==========================================

@st.cache_data(show_spinner=False)
def descargar_foto_bytes(url):
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None

def empaquetar_fotos_zip(urls_fotos, nombre_inmueble):
    buf_zip = io.BytesIO()
    with zipfile.ZipFile(buf_zip, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for i, url in enumerate(urls_fotos):
            contenido = descargar_foto_bytes(url)
            if contenido:
                zip_file.writestr(f"Foto_{i + 1}.jpg", contenido)
    buf_zip.seek(0)
    return buf_zip.getvalue()

# ==========================================
# INICIALIZACIÓN DE ESTADO PERSISTENTE
# ==========================================

if "datos_extraidos" not in st.session_state:
    st.session_state.datos_extraidos = None
if "fotos_extraidas" not in st.session_state:
    st.session_state.fotos_extraidas = []
if "zip_bytes" not in st.session_state:
    st.session_state.zip_bytes = None

# ==========================================
# INTERFAZ STREAMLIT
# ==========================================

st.title("📥 Descargador de Fotos y Ficha para Marketplace")
st.write("Extrae todas las fotos en alta resolución y genera el copy formal sin teléfonos ni mayúsculas invasivas:")

col_url, col_as = st.columns([2, 1])
with col_url:
    url_input = st.text_input("Enlace de la propiedad:", placeholder="https://nextbr.mx/propiedades/...")
with col_as:
    asesor_seleccionado = st.selectbox("Selecciona Asesor:", options=list(ASESORES.keys()), index=0)

info_asesor = ASESORES[asesor_seleccionado]

if st.button("🔍 Extraer Fotos y Generar Ficha", type="primary"):
    if not url_input.strip().startswith("http"):
        st.error("Por favor ingresa un enlace válido.")
    else:
        with st.spinner("Analizando portal y estructurando información profesional..."):
            try:
                og_t, og_d, b_txt, fotos = extraer_datos_inmueble(url_input.strip())
                datos = procesar_con_ia(url_input.strip(), og_t, og_d, b_txt, info_asesor['nombre'])
                
                st.session_state.datos_extraidos = datos
                st.session_state.fotos_extraidas = fotos
                st.session_state.zip_bytes = None  # Reset para generar con las nuevas fotos
                st.success(f"¡Extracción exitosa! Se encontraron {len(fotos)} fotografías del inmueble.")
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
        st.subheader(f"🖼️ Fotografías del Inmueble ({len(fotos)})")
        
        # Preparación del ZIP en un clic garantizado
        if st.session_state.zip_bytes is None:
            with st.spinner("Preparando archivo de descarga para todas las fotos..."):
                st.session_state.zip_bytes = empaquetar_fotos_zip(fotos, datos.get("tipo_inmueble", "Fotos"))

        colonia_archivo = re.sub(r'[\\/*?:\"<>|]', '', str(datos.get('colonia', 'Propiedad'))).strip()
        nombre_zip = f"Fotos_{datos.get('tipo_inmueble', 'Inmueble')}_{colonia_archivo}.zip"

        st.download_button(
            label=f"📦 Descargar Todas las Fotos en 1 Clic ({len(fotos)} fotos)",
            data=st.session_state.zip_bytes,
            file_name=nombre_zip,
            mime="application/zip",
            type="primary",
            use_container_width=True
        )

        st.markdown("---")
        st.markdown("##### O descarga manual foto por foto:")
        with st.expander("Ver botones individuales por fotografía", expanded=False):
            for i, f_url in enumerate(fotos):
                foto_bytes = descargar_foto_bytes(f_url)
                if foto_bytes:
                    st.download_button(
                        label=f"📥 Descargar Foto #{i + 1}",
                        data=foto_bytes,
                        file_name=f"Foto_{i + 1}.jpg",
                        mime="image/jpeg",
                        key=f"dl_btn_{i}"
                    )

    with col_copy:
        st.subheader("📋 Descripción para Marketplace")
        st.caption("Copia la descripción con el botón en la esquina superior derecha del recuadro:")
        st.code(datos.get("copy_publicitario", ""), language="markdown")
