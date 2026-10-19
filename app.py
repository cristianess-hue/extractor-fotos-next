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
        "telefono": "6622057331"
    },
    "Claudia Castro": {
        "telefono": "6621387957"
    }
}

# ==========================================
# EXTRACCIÓN Y SCRAPING
# ==========================================

def extraer_datos_inmueble(url):
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
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
                                            for cl in ['asesor', 'agente', 'agent', 'broker', 'perfil-asesor'])):
        bloque.extract()

    for s in soup(["script", "style", "noscript"]):
        s.extract()
    body_text = soup.get_text(separator="\n", strip=True)

    palabras_prohibidas = ["asesor", "agente", "agent", "staff", "perfil", "team", "logo", "avatar", "icon", "user"]
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

    return og_title, og_desc, body_text, imagenes

# ==========================================
# ESTRUCTURACIÓN CON IA (FORMATO EXACTO)
# ==========================================

def procesar_con_ia(url, og_title, og_desc, body_text, asesor_nombre, asesor_tel):
    client = genai.Client(api_key=API_KEY)
    prompt = f"""
    Eres un analista inmobiliario profesional. Extrae los datos exactos del inmueble y redacta la ficha publicitaria:
    URL: {url}
    Título: {og_title}
    Descripción: {og_desc}
    Texto: {body_text[:6000]}
    Asesor: {asesor_nombre} | Teléfono: {asesor_tel}

    Instrucciones estrictas:
    1. 'colonia': Fraccionamiento o colonia (ej: "San Benito", "Jardín Real", "Santa Fe").
    2. 'tipo_inmueble': "casa", "departamento", "oficina", "local comercial", "bodega" o "terreno" (en minúsculas).
    3. 'operacion': "renta" o "venta" (en minúsculas).
    4. 'precio': El precio con formato SIN CENTAVOS (ej: "$14,000", "$3,100,000").
    5. 'moneda': "MXN" o "USD".
    6. 'copy_publicitario': Redacta el texto iniciando OBLIGATORIAMENTE en la primera línea con este formato:
       "[tipo_inmueble] en [operacion] en colonia [colonia]"
       (Ejemplo exacto: "casa en renta en colonia san benito" o "departamento en renta en colonia jardín real").
       
       A continuación, organiza el contenido con este formato:
       💰 *Precio:* [PRECIO] [MONEDA]
       
       📐 *Superficie y Medidas:*
       (Menciona terreno y construcción si existen)
       
       ✨ *Distribución y Espacios:*
       (Puntos limpios con viñetas •)
       
       ⚡ *Equipamiento:*
       (Detalla todo el equipamiento que venga en el texto)
       
       🌳 *Área Común / Privada:*
       (Si tiene amenidades)
       
       🛡️ *Estatus Legal:*
       (Si menciona gravamen o libre de gravamen)
       
       📲 *Más información y citas:*
       *{asesor_nombre}* - Next Bienes Raíces
       WhatsApp: https://wa.me/52{asesor_tel}

       REGLA: NUNCA agregues "(si aplica)" ni paréntesis explicativos en los títulos. Omite las secciones vacías.

    Devuelve un JSON estrictamente válido:
    {{
      "colonia": "San Benito",
      "tipo_inmueble": "casa",
      "operacion": "renta",
      "precio": "$14,000",
      "moneda": "MXN",
      "copy_publicitario": "casa en renta en colonia san benito\\n\\n💰 *Precio:* $14,000 MXN..."
    }}
    """
    modelos = ['gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-3.5-flash-lite']
    for mod in modelos:
        try:
            res = client.models.generate_content(
                model=mod,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            data = json.loads(res.text)
            if "copy_publicitario" in data:
                data["copy_publicitario"] = re.sub(r'\s*\((?:si\s+aplica\vert{}si\s+existe)\)', '', data["copy_publicitario"], flags=re.IGNORECASE)
            return data
        except Exception:
            time.sleep(1)
            continue
    raise Exception("Modelos saturados temporalmente.")

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

st.title("📥 Descargador de Fotos y Ficha Descriptiva")
st.write("Ingresa el enlace de Next Bienes Raíces para extraer todas las fotos en alta resolución y generar la descripción publicitaria:")

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
        with st.spinner("Analizando portal y extrayendo imágenes..."):
            try:
                og_t, og_d, b_txt, fotos = extraer_datos_inmueble(url_input.strip())
                datos = procesar_con_ia(url_input.strip(), og_t, og_d, b_txt, asesor_seleccionado, info_asesor['telefono'])
                
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
            # Script JS que descarga foto por foto con retraso de 250ms
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
        st.subheader("📋 Descripción para Publicación")
        st.caption("Copia la descripción con el botón superior derecho del bloque:")
        st.code(datos.get("copy_publicitario", ""), language="markdown")
