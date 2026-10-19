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
# PARSEADOR LOCAL DE RESPALDO (SIN ALUCINACIONES)
# ==========================================

def armar_ficha_local(og_title, og_desc, body_text, asesor_nombre):
    texto = f"{og_title}\n{og_desc}\n{body_text}"
    texto_lower = texto.lower()

    # 1. Tipo de inmueble
    tipo = "local comercial"
    for t in ["local comercial", "departamento", "oficina", "casa", "bodega", "terreno"]:
        if t in texto_lower:
            tipo = t
            break

    # 2. Operación
    operacion = "renta" if "renta" in texto_lower else "venta"

    # 3. Colonia
    colonia = "constitucion"
    col_match = re.search(r'(?:colonia|col\.?)\s+([A-Za-zÁÉÍÓÚñáéíóú\s]+?)(?:\n|,|\.)', texto, re.I)
    if col_match:
        colonia = col_match.group(1).strip().lower()

    # 4. Precio
    p_match = re.search(r'\$\s*([0-9]{1,3}(?:,[0-9]{3})+)', texto)
    precio = f"${p_match.group(1)}" if p_match else ""

    # Extraer líneas de características reales
    lineas = [l.strip() for l in body_text.split("\n") if len(l.strip()) > 3]
    caracteristicas = []
    palabras_filtro = ["next", "inmobiliaria", "contacto", "teléfono", "whatsapp", "derechos", "aviso", "privacidad", "buscar"]
    
    for l in lineas:
        if not any(pf in l.lower() for pf in palabras_filtro) and len(l) < 120:
            if l not in caracteristicas and not l.startswith("$"):
                caracteristicas.append(l)

    copy = f"{tipo} en {operacion} en colonia {colonia}\n\n"
    if precio:
        copy += f"💰 *Precio:* {precio} MXN"
        if "iva" in texto_lower:
            copy += " (más IVA, se factura)"
        copy += "\n\n"

    copy += "✨ *Características y Detalles:*\n"
    for c in caracteristicas[:12]:
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
# ESTRUCTURACIÓN CON IA CON DIAGNÓSTICO
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre):
    if not API_KEY:
        st.warning("⚠️ No se detectó GEMINI_API_KEY en Secrets. Se usó el generador local.")
        return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

    client = genai.Client(api_key=API_KEY)
    prompt = f"""
    Eres un asesor inmobiliario profesional. Extrae TODA la información real del inmueble para Facebook Marketplace sin omitir ningún detalle.

    URL: {url}
    Título: {og_title}
    Descripción: {og_desc}
    Texto:
    {body_text[:6000]}

    Asesor: {asesor_nombre}

    REGLAS:
    1. PRIMERA LÍNEA: "[tipo_inmueble] en [operacion] en colonia [colonia]" (en minúsculas).
    2. CERO ALUCINACIONES: Incluye TODOS los detalles que vengan (estacionamientos, luz 110/trifásica, m² de terraza, lavaplatos, bodega, si es más IVA, etc.).
    3. SIN TELÉFONO NI ENLACES DE WHATSAPP.
    4. CIERRE OBLIGATORIO:
       📩 *Para más información y citas, envía un mensaje directo.*
       *{asesor_nombre}* - Next Bienes Raíces

    Estructura esperada:
    [tipo_inmueble] en [operacion] en colonia [colonia]

    💰 *Precio:* [PRECIO] [MONEDA] [IVA si aplica]

    📍 *Ubicación y Zona:*
    (Afluencia, vialidades, plusvalía)

    📐 *Medidas y Distribución:*
    • Superficie
    • Estacionamientos
    • Baños
    • Espacios especiales (terraza, bodega, etc.)

    ⚡ *Instalaciones y Servicios:*
    • Tipo de luz / equipamiento

    📝 *Condiciones:*
    • Facturación o requerimientos

    📩 *Para más información y citas, envía un mensaje directo.*
    *{asesor_nombre}* - Next Bienes Raíces

    Devuelve un JSON válido:
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
    ultimo_error = ""

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
        except Exception as e:
            ultimo_error = str(e)
            time.sleep(1)
            continue

    st.info(f"ℹ️ Conexión con IA en espera ({ultimo_error[:100]}...). Se activó el generador local automático.")
    return armar_ficha_local(og_title, og_desc, body_text, asesor_nombre)

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
