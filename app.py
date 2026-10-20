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
    t = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('utf-8')
    t = t.lower().strip()
    t = re.sub(r'[^a-z0-9]+', '_', t)
    return t.strip('_')

def generar_prefijo_foto(datos):
    operacion = limpiar_slug(datos.get("operacion", "inmueble"))
    tipo = limpiar_slug(datos.get("tipo_inmueble", "propiedad"))
    colonia = limpiar_slug(datos.get("colonia", "hermosillo"))
    return f"{operacion}_{tipo}_{colonia}"

def normalizar_oracion(texto):
    t = texto.strip()
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
# PARSEADOR LOCAL DE RESPALDO (FORMATO MARKETPLACE)
# ==========================================

def armar_ficha_local(og_title, og_desc, body_text, asesor_nombre):
    texto_combinado = f"{og_title}\n{og_desc}\n{body_text}"
    texto_lower = texto_combinado.lower()

    # 1. Tipo de inmueble
    tipo = "Casa"
    emoji_tipo = "🏠"
    for t_raw, t_nom, emo in [("local comercial", "Local comercial", "🏢"), ("departamento", "Departamento", "🏢"), 
                              ("oficina", "Oficina", "🏢"), ("casa", "Casa", "🏠"), ("bodega", "Bodega", "📦"), ("terreno", "Terreno", "📐")]:
        if t_raw in texto_lower:
            tipo = t_nom
            emoji_tipo = emo
            break

    # 2. Operación
    if "traspaso" in texto_lower or "traspasa" in texto_lower:
        operacion = "traspaso"
    elif "renta" in texto_lower or "se renta" in texto_lower:
        operacion = "renta"
    else:
        operacion = "venta"

    # 3. Colonia dinámica
    colonia = ""
    patrones_col = [
        r'(?:colonia|col\.?|fraccionamiento|fracc\.?|residencial)\s+([A-Za-zÁÉÍÓÚñáéíóú0-9\s]+?)(?:\n|,|\.|\-)',
        r'(?:en|ubicada en)\s+([A-Za-zÁÉÍÓÚñáéíóú0-9\s]+?)(?:,\s*hermosillo|\n|\.)'
    ]
    for p in patrones_col:
        match_col = re.search(p, texto_combinado, re.IGNORECASE)
        if match_col:
            c_cand = match_col.group(1).strip()
            if len(c_cand) > 2 and c_cand.lower() not in ["renta", "venta", "traspaso", "hermosillo"]:
                colonia = c_cand.title()
                break

    if not colonia:
        colonia = "Hermosillo"

    # 4. Precio
    p_match = re.search(r'\$\s*([0-9]{1,3}(?:,[0-9]{3})+)', texto_combinado)
    precio = f"${p_match.group(1)}" if p_match else ""

    lineas = [l.strip() for l in body_text.split("\n") if len(l.strip()) > 3]
    caracteristicas = []
    
    descartar = [
        "next", "inmobiliaria", "contacto", "teléfono", "whatsapp", "descripción", 
        "derechos", "aviso", "buscar", "precio", "id:", "hmor-", "hmov-", 
        "contactar vía", "enviar", "propiedades relacionadas", "completa tus datos", 
        "cancelar", "aceptar", "ver en mapa", "características", "medios baños", "iva", "factura"
    ]
    
    for l in lineas:
        l_low = l.lower()
        if any(d in l_low for d in descartar):
            continue
        if not l.startswith("$") and len(l) < 140:
            l_limpia = normalizar_oracion(l)
            if l_limpia not in caracteristicas and not any(op in l_limpia.lower() for op in ["en renta", "en venta", "en traspaso"]):
                caracteristicas.append(l_limpia)

    # Armado con el nuevo diseño
    copy = f"{tipo} en {operacion} en colonia {colonia}\n\n"
    copy += f"{emoji_tipo} {tipo.upper()} EN {operacion.upper()} EN {colonia.upper()} - {precio}\n"
    copy += f"📍 {colonia}\n"
    copy += "✨ ¡Excelente oportunidad lista para habitar o comenzar tu proyecto!\n\n"

    for c in caracteristicas:
        c_low = c.lower()
        if "recámara" in c_low or "recamara" in c_low or "habitaci" in c_low:
            copy += f"🛏️ {c}\n"
        elif "baño" in c_low:
            copy += f"🚿 {c}\n"
        elif "cochera" in c_low or "auto" in c_low or "estacionamiento" in c_low:
            copy += f"🚗 {c}\n"
        elif "minisplit" in c_low or "aire" in c_low or "clima" in c_low:
            copy += f"❄️ Climatización: {c}\n"
        elif "lavad" in c_low:
            copy += f"🧺 Área de lavado: {c}\n"
        elif "agua" in c_low or "boiler" in c_low or "gas" in c_low or "luz" in c_low:
            copy += f"💧 Instalaciones y servicios: {c}\n"
        elif "internet" in c_low or "wifi" in c_low:
            copy += f"📶 {c}\n"
        elif "mantenimiento" in c_low:
            copy += f"🧹 {c}\n"
        else:
            copy += f"🏠 {c}\n"

    copy += f"\n📲 Más información y citas por mensaje directo.\n\n*{asesor_nombre}* - Next Bienes Raíces"

    return {
        "colonia": colonia,
        "tipo_inmueble": tipo,
        "operacion": operacion,
        "precio": precio,
        "copy_publicitario": copy
    }

# ==========================================
# ESTRUCTURACIÓN CON IA (NUEVO FORMATO EMOJIS)
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre):
    if not API_KEY:
        return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

    client = genai.Client(api_key=API_KEY)
    prompt = f"""
    Eres un asesor inmobiliario profesional de élite. Redacta una ficha publicitaria moderna, limpia y atractiva para Facebook Marketplace usando emojis temáticos por categoría.

    DATOS DE LA PROPIEDAD:
    URL: {url}
    Título: {og_title}
    Descripción: {og_desc}
    Texto completo:
    {body_text[:6000]}

    Asesor: {asesor_nombre}

    REGLAS CRÍTICAS:
    1. PROHIBIDO TOTALMENTE EL IVA: NUNCA pongas "más IVA", "IVA incluido", "se factura" ni nada relacionado con impuestos. El precio va directo y limpio.
    2. OPERACIÓN: Si dice "traspaso" o "se traspasa", la operación DEBE ser "traspaso" (en minúsculas), aunque el portal diga venta. Si no, usa "renta" o "venta".
    3. COLONIA: Extrae el nombre exacto de la colonia o residencial (ej: Praga Residencial, Balderrama, San Benito, Constitución).
    4. SIN TELÉFONO NI LINKS: No enlaces wa.me, ni teléfonos directos.
    5. CERO ALUCINACIONES: Incluye todos los datos reales (recámaras, baños, cochera, electrodomésticos, climas, terraza, servicios incluidos).

    ESTRUCTURA EXACTA DE 'copy_publicitario' QUE DEBES GENERAR:
    [Tipo de inmueble] en [operación] en colonia [Nombre de la Colonia]

    [EMOJI_TIPO] [TIPO DE INMUEBLE EN MAYÚSCULAS] EN [OPERACIÓN EN MAYÚSCULAS] EN [COLONIA EN MAYÚSCULAS] - [PRECIO]
    📍 [Colonia]
    ✨ [Gancho comercial breve y atractivo basado en si está amueblado, equipado, listo para habitar, excelente ubicación o zona de alta plusvalía]

    (Luego agrupa la información del texto usando estos emojis exactos según aplique):
    🛏️ [Detalle de recámaras, clósets o camas si tiene]
    🚿 [Detalle de baños completos o medios baños]
    🏠 [Planta, sala, comedor, cocina integral equipada y espacios principales]
    🚗 [Cochera o estacionamientos con cantidad de autos]

    ❄️ Climatización: [Minisplits, aire central, etc. si se menciona]
    📺 Entretenimiento / Tecnología: [TVs, sistemas, etc. si se menciona]
    🧺 Área de lavado: [Lavadora, secadora, lavadero, boiler, etc. si se menciona]
    💧 Instalaciones y servicios: [Filtro de agua, cisterna, hidroneumático, gas, luz 110/trifásica]
    💡 Exteriores y seguridad: [Alberca, terraza, rejas, buzón, acceso controlado si aplica]

    (Líneas separadas si incluye servicios):
    🧹 Mantenimiento incluido (si aplica en el texto)
    📶 Internet incluido (si aplica en el texto)
    💧 Agua incluida (si aplica en el texto)

    📲 Más información y citas por mensaje directo.

    *{asesor_nombre}* - Next Bienes Raíces

    Devuelve un JSON estrictamente válido:
    {{
      "colonia": "...",
      "tipo_inmueble": "...",
      "operacion": "...",
      "precio": "...",
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
                copy = re.sub(r'(?i)\n.*(?:id\s*:\s*hmo[a-z0-9_-]+|hmor-[0-9]+|hmov-[0-9]+).*', '', copy)
                # Garantizar cero IVA en cualquier texto
                copy = re.sub(r'\s*\((?:más\s+iva\vert{}se\s+factura\vert{}facturable\vert{}iva)[^)]*\)', '', copy, flags=re.IGNORECASE)
                copy = re.sub(r'(?i)•.*(?:más\s+iva|factura|facturación).*\n?', '', copy)
                data["copy_publicitario"] = copy.strip()
            return data
        except Exception:
            time.sleep(1)
            continue

    return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

# ==========================================
# DESCARGA PROXY (CONVERSIÓN A BYTES)
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
