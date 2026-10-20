import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
import base64
import unicodedata
import streamlit.components.v1 as components
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
# UTILIDADES DE TEXTO Y NOMENCLATURA
# ==========================================

def limpiar_slug(texto):
    if not texto:
        return "propiedad"
    t = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    t = t.lower().strip()
    t = re.sub(r'[^a-z0-9]+', '_', t)
    return t.strip('_')

def generar_prefijo_foto(datos):
    operacion = limpiar_slug(datos.get("operacion", "inmueble"))
    tipo = limpiar_slug(datos.get("tipo_inmueble", "propiedad"))
    colonia = limpiar_slug(datos.get("colonia", "hermosillo"))
    return f"{operacion}_{tipo}_{colonia}"

def normalizar_oracion(texto):
    t = str(texto).strip()
    t = re.sub(r'^[\*\•\-\–\—\s\.\:\,\;]+', '', t).strip()
    t = re.sub(r'^[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\s]+', '', t).strip()
    if not t:
        return ""
    if t.isupper() or sum(1 for c in t if c.isupper()) > len(t) * 0.6:
        t = t.lower()
        t = t[0].upper() + t[1:]
    reemplazos = {
        r'\bmorelos\b': 'Morelos',
        r'\bhermosillo\b': 'Hermosillo',
        r'\bsonora\b': 'Sonora',
        r'\bmexico\b': 'México',
        r'\bblvd\.?\b': 'Blvd.',
        r'\biva\b': 'IVA',
        r'\bm2\b': 'm²',
        r'\bfovissste\b': 'FOVISSSTE',
        r'\binfonavit\b': 'INFONAVIT'
    }
    for patron, repl in reemplazos.items():
        t = re.sub(patron, repl, t, flags=re.IGNORECASE)
    return t

def detectar_tipo_inmueble(url, og_title, body_text):
    fuente_url_tit = f"{url} {og_title}".lower()
    
    if "casa" in fuente_url_tit:
        return "Casa", "🏠"
    if "local" in fuente_url_tit:
        return "Local comercial", "🏢"
    if "oficina" in fuente_url_tit:
        return "Oficina", "🏢"
    if "bodega" in fuente_url_tit:
        return "Bodega", "📦"
    if "terreno" in fuente_url_tit:
        return "Terreno", "📐"
    if "departamento" in fuente_url_tit or "depto" in fuente_url_tit:
        return "Departamento", "🏢"

    body_low = body_text.lower()
    if "departamento" in body_low:
        return "Departamento", "🏢"
    if "casa" in body_low:
        return "Casa", "🏠"
    if "local comercial" in body_low:
        return "Local comercial", "🏢"
    if "oficina" in body_low:
        return "Oficina", "🏢"
    if "bodega" in body_low:
        return "Bodega", "📦"
    if "terreno" in body_low:
        return "Terreno", "📐"

    return "Casa", "🏠"

def extraer_colonia_real(url, og_title, body_text):
    """
    Extrae la colonia dando prioridad a la URL y al título real del asesor,
    evitando los errores de captura del sistema interno de Next.
    """
    # 1. De la URL directa (ej: departamento-en-renta-en-col-prados-del-sol-34931)
    m_url = re.search(r'(?:en-col-|en-colonia-|en-fracc-|en-fraccionamiento-|en-residencial-|en-)([a-z0-9\-]+?)(?:-[0-9]{3,7})?$', url.lower().split('?')[0])
    if m_url:
        cand_url = m_url.group(1).replace('-', ' ').strip()
        cand_url = re.sub(r'\b(?:casa|departamento|local|oficina|bodega|terreno|renta|venta|traspaso|col|hermosillo)\b', '', cand_url).strip()
        if len(cand_url) > 2:
            return cand_url.title()

    # 2. Del Título Principal (ej: DEPARTAMENTO EN RENTA EN COL. PRADOS DEL SOL)
    m_tit = re.search(r'en\s+(?:col\.?\s+|colonia\s+|fracc\.?\s+|residencial\s+)?([A-Za-zÁÉÍÓÚñáéíóú0-9\s]+)', og_title, re.IGNORECASE)
    if m_tit:
        cand_tit = m_tit.group(1).strip()
        cand_tit = re.sub(r'(?i)\b(?:casa|departamento|local|oficina|bodega|terreno|renta|venta|traspaso|col|colonia|en)\b', '', cand_tit).strip()
        if len(cand_tit) > 2 and cand_tit.lower() not in ["hermosillo", "sonora"]:
            return cand_tit.title()

    # 3. De la línea de dirección geográfica (ej: Prados del Sol, 83100 Hermosillo, Son., México)
    m_dir = re.search(r'([A-Za-zÁÉÍÓÚñáéíóú0-9\s]+?),\s*[0-9]{5}\s+Hermosillo', body_text, re.IGNORECASE)
    if m_dir:
        cand_dir = m_dir.group(1).strip()
        cand_dir = re.sub(r'(?i)\b(?:ubicación|ubicacion|colonia|col\.?)\b', '', cand_dir).strip(' :.-')
        if len(cand_dir) > 2:
            return cand_dir.title()

    # 4. Búsqueda en texto por etiquetas
    m_txt = re.search(r'(?:en\s+col\.?\s+|colonia\s+|fraccionamiento\s+|residencial\s+)([A-Za-zÁÉÍÓÚñáéíóú0-9\s]+?)(?:,|\.|\n|-)', body_text, re.IGNORECASE)
    if m_txt:
        cand_txt = m_txt.group(1).strip()
        cand_txt = re.sub(r'(?i)\b(?:casa|departamento|renta|venta|hermosillo|sonora)\b', '', cand_txt).strip()
        if len(cand_txt) > 2:
            return cand_txt.title()

    return "Hermosillo"

# ==========================================
# EXTRACCIÓN Y SCRAPING
# ==========================================

def extraer_datos_inmueble(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    resp = requests.get(url, headers=headers, timeout=(5, 10))
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
                                            for cl in ['perfil-asesor', 'agent-box', 'agent-info', 'author-box', 'related-properties'])):
        bloque.extract()

    for s in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        s.extract()

    contenedor_desc = (
        soup.find(class_=re.compile(r'(property-description|descripcion|listing-detail|contenido-propiedad)', re.I)) or
        soup.find(id=re.compile(r'(property-description|descripcion|listing-detail)', re.I))
    )
    
    if contenedor_desc:
        texto_principal = contenedor_desc.get_text(separator="\n", strip=True)
    else:
        texto_principal = soup.get_text(separator="\n", strip=True)

    patron_corte = re.search(r'(?i)\b(?:id\s*:\s*hmo[a-z0-9_-]+|hmo[rv]-[0-9]+|id\s+de\s+la\s+propiedad)', texto_principal)
    if patron_corte:
        texto_principal = texto_principal[:patron_corte.start()].strip()

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
# PARSEADOR LOCAL LIMPIO
# ==========================================

def armar_ficha_local(url, og_title, og_desc, body_text, asesor_nombre):
    texto_combinado = f"{og_title}\n{og_desc}\n{body_text}"
    texto_lower = texto_combinado.lower()

    tipo, emoji_tipo = detectar_tipo_inmueble(url, og_title, body_text)

    fuente_operacion = f"{url} {og_title}".lower()
    if "traspaso" in fuente_operacion or "traspasa" in texto_lower:
        operacion = "traspaso"
    elif "renta" in fuente_operacion or "se renta" in texto_lower:
        operacion = "renta"
    else:
        operacion = "venta"

    # Extracción corregida de la colonia real
    colonia = extraer_colonia_real(url, og_title, body_text)

    p_match = re.search(r'\$\s*([0-9]{1,3}(?:,[0-9]{3})+)', texto_combinado)
    precio = f"${p_match.group(1)}" if p_match else ""

    lineas = [l.strip() for l in body_text.split("\n") if len(l.strip()) > 2]
    
    descartar = [
        "next", "inmobiliaria", "contacto", "teléfono", "whatsapp", "descripción", 
        "derechos", "aviso", "buscar", "precio", "id:", "hmor-", "hmov-", 
        "contactar vía", "enviar", "propiedades relacionadas", "completa tus datos", 
        "cancelar", "aceptar", "ver en mapa", "características", "medios baños", "iva", "factura",
        "hermosillo, son", "méxico", "mexico", "83", "c.p.", "código postal", "distribución"
    ]
    
    superficie_texto = ""
    recamaras = []
    banos = []
    areas = []
    estac = []
    seguridad = []
    equipamiento = []
    servicios = []
    otros = []

    for l in lineas:
        l_low = l.lower().strip()
        
        # Omitir la línea del sistema interno de Next con datos contradictorios
        if "hermosillo" in l_low and ("terreno" in l_low or "construcción" in l_low or "m²" in l_low or "m2" in l_low):
            medidas_limpias = re.findall(r'[0-9]+(?:\.[0-9]+)?\s*m[²2]\s*(?:de\s+)?(?:terreno|construcción)', l, re.IGNORECASE)
            if medidas_limpias and not superficie_texto:
                superficie_texto = " | ".join(medidas_limpias)
            continue

        if l_low in ["venta", "renta", "traspaso", colonia.lower(), f"colonia {colonia.lower()}", "distribución", "equipamiento:"]:
            continue
        if any(d in l_low for d in descartar) or l.startswith("$") or len(l) > 130:
            continue
        if "ubicación:" in l_low or "ubicada en" in l_low:
            continue

        l_limp = normalizar_oracion(l)
        if not l_limp:
            continue

        if ("terreno:" in l_low or "construcción:" in l_low) and not superficie_texto:
            superficie_texto = l_limp
        elif "recámara" in l_low or "habitaci" in l_low:
            recamaras.append(l_limp)
        elif "baño" in l_low or "regadera" in l_low or "w.c" in l_low:
            banos.append(l_limp)
        elif "cochera" in l_low or "auto" in l_low or "estacionamiento" in l_low:
            estac.append(l_limp)
        elif any(w in l_low for w in ["internet", "wifi", "agua incluida", "mantenimiento incluido"]):
            servicios.append(l_limp)
        elif any(w in l_low for w in ["minisplit", "clima", "parrilla", "refrigerador", "tv", "colchón", "amueblado", "equipado"]):
            equipamiento.append(l_limp)
        elif any(w in l_low for w in ["cámara", "camara", "sensor", "alarma", "reja", "seguridad"]):
            seguridad.append(l_limp)
        elif any(w in l_low for w in ["sala", "comedor", "cocina", "estancia", "patio", "lavandería", "lavanderia"]):
            areas.append(l_limp)
        else:
            if not any(op in l_limp.lower() for op in ["en venta", "en renta", "en traspaso"]):
                otros.append(l_limp)

    copy = f"{emoji_tipo} {tipo.upper()} EN {operacion.upper()} EN {colonia.upper()}{' - ' + precio if precio else ''}\n"
    copy += "✨ ¡Excelente propiedad lista para habitar!\n\n"

    if superficie_texto:
        copy += f"📐 Superficie: {superficie_texto}\n\n"

    for r in recamaras:
        copy += f"🛏️ {r}\n"
    for b in banos:
        copy += f"🚿 {b}\n"
    for e in estac:
        copy += f"🚗 {e}\n"

    if areas:
        copy += f"🏠 Espacios: {', '.join(areas)}\n"

    if equipamiento:
        copy += f"\n⚡ Equipamiento:\n"
        for eq in equipamiento[:6]:
            copy += f"• {eq}\n"

    if servicios:
        copy += f"\n📶 Servicios incluidos:\n"
        for sv in servicios:
            copy += f"• {sv}\n"

    if seguridad:
        copy += f"\n🔒 Seguridad:\n"
        for s in seguridad:
            copy += f"• {s}\n"

    copy += f"\n📲 Más información y citas por mensaje directo.\n\n*{asesor_nombre}* - Next Bienes Raíces"

    return {
        "colonia": colonia,
        "tipo_inmueble": tipo,
        "operacion": operacion,
        "precio": precio,
        "copy_publicitario": copy.strip()
    }

# ==========================================
# ESTRUCTURACIÓN CON IA
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre):
    tipo_detectado, _ = detectar_tipo_inmueble(url, og_title, body_text)
    colonia_detectada = extraer_colonia_real(url, og_title, body_text)

    if not API_KEY:
        return armar_ficha_local(url, og_title, og_desc, body_text, asesor_nombre)

    prompt = f"""
    Eres un asesor inmobiliario profesional de élite. Redacta una ficha publicitaria directa y atractiva para Facebook Marketplace.

    DATOS DE LA PROPIEDAD:
    URL: {url}
    Título original: {og_title}
    Tipo de inmueble confirmado: {tipo_detectado}
    Colonia real y confirmada: {colonia_detectada}
    Descripción: {og_desc}
    Texto:
    {body_text[:4000]}

    Asesor: {asesor_nombre}

    REGLAS DE ORO:
    1. LA COLONIA ES: "{colonia_detectada}". NUNCA uses nombres erróneos del formulario como "Misión del Sol" si el título o URL dicen claramente "{colonia_detectada}".
    2. ENCABEZADO EXACTO:
       [EMOJI_TIPO] {tipo_detectado.upper()} EN [OPERACIÓN EN MAYÚSCULAS] EN {colonia_detectada.upper()} - [PRECIO]
       PROHIBIDO duplicar palabras o frases.
    3. CERO REPETICIÓN: No pongas antes ninguna línea suelta en minúsculas, ni pongas "📍 Colonia" abajo del título.
    4. CERO LÍNEAS CRUDAS: Si en el texto dice "... en Hermosillo , ... , 60.00 m²...", pon únicamente "📐 Superficie: 60 m² construcción".
    5. CERO IVA NI FACTURAS.

    ESTRUCTURA EXACTA DE 'copy_publicitario':
    [EMOJI_TIPO] {tipo_detectado.upper()} EN [OPERACIÓN EN MAYÚSCULAS] EN {colonia_detectada.upper()} - [PRECIO]
    ✨ [Frase breve gancho atractiva]

    📐 [Metros de terreno / construcción limpios si se indican]

    🛏️ [Cantidad de recámaras y detalles]
    🚿 [Cantidad de baños]
    🚗 [Cochera o estacionamiento]
    🏠 Espacios: [Agrupa sala, comedor, cocina, etc.]

    (Si cuenta con equipamiento, servicios o seguridad):
    ⚡ Equipamiento: [Minisplits, estufa, refrigerador, amueblado, etc.]
    📶 Servicios incluidos: [Internet, agua, mantenimiento si aplica]
    🔒 Seguridad: [Cámaras, portón eléctrico, etc. si aplica]

    📲 Más información y citas por mensaje directo.

    *{asesor_nombre}* - Next Bienes Raíces

    Devuelve un JSON estrictamente válido:
    {{
      "colonia": "{colonia_detectada}",
      "tipo_inmueble": "{tipo_detectado}",
      "operacion": "renta",
      "precio": "$...",
      "moneda": "MXN",
      "copy_publicitario": "..."
    }}
    """

    try:
        client = genai.Client(api_key=API_KEY)
        res = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        data = json.loads(res.text)
        if "copy_publicitario" in data:
            copy = data["copy_publicitario"]
            copy = re.sub(r'https?://[^\s]+', '', copy)
            copy = re.sub(r'\bwa\.me[^\s]*', '', copy)
            copy = re.sub(r'\b\d{10}\b', '', copy)
            copy = re.sub(r'(?i)\n.*(?:id\s*:\s*hmo[a-z0-9_-]+|hmor-[0-9]+|hmov-[0-9]+).*', '', copy)
            copy = re.sub(r'\s*\((?:más\s+iva\vert{}se\s+factura\vert{}facturable\vert{}iva)[^)]*\)', '', copy, flags=re.IGNORECASE)
            copy = re.sub(r'([🏠🛏️🚿🚗❄️🌳🔹🔺])\s*[\*\•\-]?\s*([🏠🛏️🚿🚗❄️🌳])', r'\1', copy)
            copy = re.sub(r'^(?:casa|departamento|local|oficina|bodega|terreno)\s+en\s+(?:renta|venta|traspaso)[^\n]*\n+', '', copy, flags=re.IGNORECASE)
            
            primera_linea = copy.split("\n")[0]
            primera_limpia = re.sub(r'(?i)\b(RENTA|VENTA|TRASPASO)\s+EN\s+\1\b', r'\1', primera_linea)
            primera_limpia = re.sub(r'(?i)\bEN\s+COL\b', '', primera_limpia).strip()
            copy = copy.replace(primera_linea, primera_limpia, 1)

            data["colonia"] = colonia_detectada
            data["tipo_inmueble"] = tipo_detectado
            data["copy_publicitario"] = copy.strip()
            return data
    except Exception:
        pass

    return armar_ficha_local(url, og_title, og_desc, body_text, asesor_nombre)

# ==========================================
# DESCARGA PROXY (CONVERSIÓN A BYTES)
# ==========================================

@st.cache_data(show_spinner=False)
def descargar_foto_bytes(url):
    try:
        r = requests.get(url, timeout=(3, 8), headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None

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
st.write("Extrae todas las fotos en alta resolución con nombres organizados y genera el copy formal:")

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
                st.session_state.descargar_ahora = False
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

    prefijo_inmueble = generar_prefijo_foto(datos)

    col_descargas, col_copy = st.columns([1, 1])

    with col_descargas:
        st.subheader(f"🖼️ Fotografías Encontradas ({len(fotos)})")
        st.caption(f"📌 Nomenclatura asignada: `foto_[#]_{prefijo_inmueble}.jpg`")
        
        if st.button("🚀 Descargar Todas las Fotos Sueltas (Sin ZIP)", type="primary", use_container_width=True):
            st.session_state.descargar_ahora = True

        if st.session_state.descargar_ahora:
            with st.spinner("Descargando imágenes organizadas a tu equipo..."):
                lista_archivos = []
                for i, u in enumerate(fotos):
                    b = descargar_foto_bytes(u)
                    if b:
                        b64 = base64.b64encode(b).decode('utf-8')
                        nombre_foto = f"foto_{i + 1}_{prefijo_inmueble}.jpg"
                        lista_archivos.append({
                            "nombre": nombre_foto,
                            "data": f"data:image/jpeg;base64,{b64}"
                        })

                payload_json = json.dumps(lista_archivos)

                js_script = f"""
                <div id="status_descarga" style="padding: 10px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; color: #166534; font-size: 14px;">
                    ⏳ Descargando {len(lista_archivos)} imágenes individuales como <b>foto_[#]_{prefijo_inmueble}.jpg</b>...
                </div>
                <script>
                (async function() {{
                    const items = {payload_json};
                    for (let i = 0; i < items.length; i++) {{
                        const link = document.createElement('a');
                        link.href = items[i].data;
                        link.download = items[i].nombre;
                        document.body.appendChild(link);
                        link.click();
                        document.body.removeChild(link);
                        await new Promise(res => setTimeout(res, 350));
                    }}
                    document.getElementById('status_descarga').innerHTML = '✅ <b>¡Descarga completada!</b> Revisa tu carpeta de Descargas.';
                }})();
                </script>
                """
                components.html(js_script, height=90)

        st.markdown("---")
        st.markdown("##### Descarga manual foto por foto (Siempre visibles):")
        
        for i, f_url in enumerate(fotos):
            foto_bytes = descargar_foto_bytes(f_url)
            if foto_bytes:
                nombre_individual = f"foto_{i + 1}_{prefijo_inmueble}.jpg"
                st.download_button(
                    label=f"📥 Descargar {nombre_individual}",
                    data=foto_bytes,
                    file_name=nombre_individual,
                    mime="image/jpeg",
                    key=f"dl_btn_{i}",
                    use_container_width=True
                )

    with col_copy:
        st.subheader("📋 Descripción para Marketplace")
        st.caption("Copia la descripción con el botón en la esquina superior derecha del recuadro:")
        st.code(datos.get("copy_publicitario", ""), language="markdown")
