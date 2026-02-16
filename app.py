import streamlit as st
import anthropic
import json
import base64
import requests
import re

st.set_page_config(page_title="Generador de PBIs", page_icon="📋", layout="wide")

SYSTEM_PROMPT = """Eres un asistente experto en Product Management que genera Product Backlog Items (PBIs) para Azure DevOps.
Tu audiencia son desarrolladores y QA que deben poder implementar y testear sin necesidad de preguntar al PM.

EL INPUT DEL USUARIO PUEDE SER:
- Texto breve e informal. Tu trabajo es estructurarlo y completarlo.
- Una descripción larga de una feature completa. Tu trabajo es proponer la división óptima.
- Capturas de prototipo de Figma. Analízalas en detalle: componentes, estados, textos, validaciones visibles, flujos.

REGLAS DE DIVISIÓN:
- Evalúa la complejidad REAL. Un cambio de validación puntual = 1 PBI.
- Solo divide cuando hay flujos claramente independientes.
- En "summary", JUSTIFICA tu decisión.

FORMATO DE CADA PBI:
- Título: [Módulo] - [Feature] - US X.X - [Acción concreta y alcance]
- Objetivo: UNA frase del por qué
- Historia de Usuario:
  * COMO [rol con contexto]
  * CUANDO [ruta navegación completa: Sección → Subsección → Pantalla]
  * ENTONCES [acción específica y resultado esperado]
  * PARA [beneficio concreto]
- Criterios de Aceptación — NIVEL DE DETALLE ADECUADO:
  * Happy Path: cada AC debe describir un comportamiento verificable. Incluye datos concretos cuando los haya (nombres de campos, valores, estados, textos de botones visibles en el prototipo).
  * Validaciones: reglas de negocio, límites, formatos, estados no permitidos. Sé específico con los mensajes de error si son visibles en el prototipo.
  * Errores: comportamiento ante fallos de red, datos vacíos, timeouts — solo si son relevantes para esta funcionalidad.
  * Si una funcionalidad tiene múltiples columnas, campos, estados o comportamientos, DETALLA cada uno. No resumas "se muestran los datos" cuando puedes especificar qué datos, en qué columnas, con qué formato.
- Prototipo: referencia a cada captura indicando EXACTAMENTE qué muestra de relevante para este PBI. Formato: "(Captura X) Muestra [descripción detallada de lo relevante]". Indica el número de captura en orden.
- Dependencias: entre PBIs si los hay
- Notas Técnicas: preguntas concretas para desarrollo, no obviedades

NO seas escueto: un PBI con 2 ACs genéricos no sirve para implementar. Pero tampoco infles con ACs redundantes o que repiten la historia de usuario.
La regla es: ¿un desarrollador puede implementar esto sin preguntarme nada? ¿QA puede escribir los test cases directamente de los ACs?

RESPONDE SOLO JSON válido sin backticks:
{
  "summary": "Justificación de la división",
  "pbis": [{
    "title": "...", "objective": "...", "role": "...", "when": "...", "then": "...", "benefit": "...",
    "happy_path": ["AC1: ..."], "validations": ["AC-V1: ..."], "error_states": ["AC-E1: ..."],
    "prototype_refs": ["(Captura 1) Muestra..."], "dependencies": [], "tech_notes": ["..."]
  }]
}"""


# ========== AZURE DEVOPS INTEGRATION ==========

def get_azure_connection():
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication
    credentials = BasicAuthentication("", st.secrets["AZURE_PAT"])
    return Connection(
        base_url=f"https://dev.azure.com/{st.secrets['AZURE_ORG']}",
        creds=credentials
    )


def push_pbi_to_azure(pbi, iteration_path=None, area_path=None, parent_id=None, figma_urls=None, figma_link=None):
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation
    conn = get_azure_connection()
    wit_client = conn.clients.get_work_item_tracking_client()
    project = st.secrets["AZURE_PROJECT"]

    html_desc = pbi_to_html(pbi, figma_urls, figma_link)

    patch = [
        {"op": "add", "path": "/fields/System.Title", "value": pbi["title"]},
        {"op": "add", "path": "/fields/System.Description", "value": html_desc},
    ]

    if iteration_path:
        patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
    if area_path:
        patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
    if parent_id:
        patch.append({
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"https://dev.azure.com/{st.secrets['AZURE_ORG']}/_apis/wit/workItems/{parent_id}",
            }
        })

    patch_ops = [JsonPatchOperation(**p) for p in patch]
    return wit_client.create_work_item(document=patch_ops, project=project, type="Product Backlog Item")


# ========== FIGMA INTEGRATION ==========

def parse_figma_url(url):
    url = url.strip()
    proto_match = re.search(r'figma\.com/proto/([a-zA-Z0-9]+)', url)
    design_match = re.search(r'figma\.com/(?:design|file)/([a-zA-Z0-9]+)', url)

    file_key = None
    if proto_match:
        file_key = proto_match.group(1)
    elif design_match:
        file_key = design_match.group(1)

    if not file_key:
        return None, None

    node_ids = set()
    if 'node-id=' in url:
        nid = re.search(r'node-id=([^&]+)', url)
        if nid:
            node_ids.add(nid.group(1).replace('-', ':'))
    if 'starting-point-node-id=' in url:
        nid = re.search(r'starting-point-node-id=([^&]+)', url)
        if nid:
            node_ids.add(nid.group(1).replace('-', ':'))

    return file_key, list(node_ids)


def get_figma_images(file_key, node_ids, figma_token):
    headers = {"X-Figma-Token": figma_token}
    ids_str = ",".join(node_ids)
    resp = requests.get(
        f"https://api.figma.com/v1/images/{file_key}?ids={ids_str}&format=png&scale=2",
        headers=headers
    )

    if resp.status_code != 200:
        st.error(f"Error exportando imágenes de Figma: {resp.status_code}")
        return []

    data = resp.json()
    images = []
    for node_id, img_url in data.get("images", {}).items():
        if img_url:
            img_resp = requests.get(img_url)
            if img_resp.status_code == 200:
                b64 = base64.b64encode(img_resp.content).decode("utf-8")
                images.append({
                    "data": b64,
                    "media_type": "image/png",
                    "node_id": node_id,
                    "url": img_url
                })
    return images


# ========== PBI FORMATTING ==========

def pbi_to_html(p, figma_image_urls=None, figma_link=None):
    h = f"<h2>{p['title']}</h2>"
    h += f"<h3>🎯 Objetivo</h3><p>{p['objective']}</p>"
    h += "<h3>👤 Historia de Usuario</h3>"
    h += f"<p><b>Como</b> {p['role']}<br><b>Cuando</b> {p['when']}<br><b>Entonces</b> {p['then']}<br><b>Para</b> {p['benefit']}</p>"
    h += "<h3>✅ Criterios de Aceptación</h3><h4>Happy Path</h4><ul>"
    for ac in p.get("happy_path", []):
        h += f"<li>{ac}</li>"
    h += "</ul>"
    if p.get("validations"):
        h += "<h4>Validaciones y Edge Cases</h4><ul>"
        for v in p["validations"]:
            h += f"<li>{v}</li>"
        h += "</ul>"
    if p.get("error_states"):
        h += "<h4>Estados de Error</h4><ul>"
        for e in p["error_states"]:
            h += f"<li>{e}</li>"
        h += "</ul>"
    # Prototype section with Figma link and embedded images
    h += "<h3>🖼️ Prototipo</h3>"
    if figma_link:
        h += f'<p><b>Figma:</b> <a href="{figma_link}">{figma_link}</a></p>'
    if p.get("prototype_refs"):
        for i, r in enumerate(p["prototype_refs"]):
            h += f"<p>{r}</p>"
            cap_match = re.search(r'[Cc]aptura\s*(\d+)', r)
            if cap_match and figma_image_urls:
                cap_idx = int(cap_match.group(1)) - 1
                if 0 <= cap_idx < len(figma_image_urls):
                    h += f'<p><img src="{figma_image_urls[cap_idx]}" style="max-width:800px;border:1px solid #ddd;border-radius:4px;" /></p>'
    if p.get("dependencies"):
        h += "<h3>🔗 Dependencias</h3><ul>"
        for d in p["dependencies"]:
            h += f"<li>{d}</li>"
        h += "</ul>"
    if p.get("tech_notes"):
        h += "<h3>💡 Notas Técnicas</h3><ul>"
        for n in p["tech_notes"]:
            h += f"<li>{n}</li>"
        h += "</ul>"
    return h


# ========== PBI GENERATION ==========

def generate_pbis(module, feature, description, context, images):
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])

    user_content = []
    text = f"MÓDULO: {module or 'No especificado'}\nFEATURE: {feature or 'No especificada'}\n\nDESCRIPCIÓN:\n{description}"
    if context:
        text += f"\n\nCONTEXTO TÉCNICO:\n{context}"
    if images:
        text += f"\n\nSe adjuntan {len(images)} captura(s) del prototipo (Captura 1, 2...). Analízalas y referéncialas en los PBIs."

    user_content.append({"type": "text", "text": text})

    for img in images:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}
        })

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )

    raw = "".join(block.text for block in response.content if block.type == "text")
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


# ========== RENDER PBI CARD ==========

def render_pbi_card(pbi, idx, total):
    figma_urls = []
    figma_link = st.session_state.get("figma_url", None)
    if "figma_images" in st.session_state:
        figma_urls = [img.get("url", "") for img in st.session_state["figma_images"]]

    html_content = pbi_to_html(pbi, figma_urls, figma_link)

    # Buttons row
    col_copy, col_push = st.columns([1, 1])

    with col_copy:
        st.components.v1.html(f"""
        <div>
            <button onclick="copyHtml()" style="background:#6366f1;color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:13px;font-weight:600;">
                📋 Copiar para Azure
            </button>
            <span id="status_{idx}" style="margin-left:8px;font-size:13px;color:#10b981;display:none;">✓ Copiado</span>
        </div>
        <script>
        async function copyHtml() {{
            const html = {json.dumps(html_content)};
            const plain = {json.dumps(pbi['title'])};
            try {{
                await navigator.clipboard.write([
                    new ClipboardItem({{
                        "text/html": new Blob([html], {{type: "text/html"}}),
                        "text/plain": new Blob([plain], {{type: "text/plain"}})
                    }})
                ]);
            }} catch(e) {{
                const div = document.createElement("div");
                div.innerHTML = html;
                div.style.cssText = "position:fixed;left:-9999px;opacity:0";
                document.body.appendChild(div);
                const range = document.createRange();
                range.selectNodeContents(div);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand("copy");
                sel.removeAllRanges();
                document.body.removeChild(div);
            }}
            const s = document.getElementById("status_{idx}");
            s.style.display = "inline";
            setTimeout(() => s.style.display = "none", 2000);
        }}
        </script>
        """, height=50)

    azure_available = all(k in st.secrets for k in ["AZURE_PAT", "AZURE_ORG", "AZURE_PROJECT"])

    with col_push:
        if azure_available:
            with st.popover("🚀 Push to Azure", use_container_width=True):
                st.markdown(f"**Crear en Azure DevOps:**\n\n`{pbi['title']}`")
                iteration = st.text_input("Iteration Path (Sprint)", placeholder="Ej: SWAre\\2026\\PRODUCT\\Q1\\IT3", key=f"iter_{idx}")
                area = st.text_input("Area Path", placeholder="Ej: SWAre\\Time", key=f"area_{idx}")
                parent = st.text_input("Parent Feature ID (opcional)", placeholder="Ej: 177040", key=f"parent_{idx}")

                if st.button("✅ Crear PBI", key=f"push_{idx}", type="primary", use_container_width=True):
                    with st.spinner("Creando en Azure DevOps..."):
                        try:
                            parent_id = int(parent) if parent.strip() else None
                            result = push_pbi_to_azure(
                                pbi,
                                iteration_path=iteration if iteration.strip() else None,
                                area_path=area if area.strip() else None,
                                parent_id=parent_id,
                                figma_urls=figma_urls,
                                figma_link=figma_link
                            )
                            st.success(f"✅ PBI creado — ID: **{result.id}** — [Abrir en Azure](https://dev.azure.com/{st.secrets['AZURE_ORG']}/{st.secrets['AZURE_PROJECT']}/_workitems/edit/{result.id})")
                        except Exception as e:
                            st.error(f"Error: {e}")

    # Editable fields
    pbi["objective"] = st.text_input("🎯 Objetivo", pbi["objective"], key=f"obj_{idx}")

    st.markdown("**👤 Historia de Usuario**")
    pbi["role"] = st.text_input("Como", pbi["role"], key=f"role_{idx}")
    pbi["when"] = st.text_input("Cuando", pbi["when"], key=f"when_{idx}")
    pbi["then"] = st.text_input("Entonces", pbi["then"], key=f"then_{idx}")
    pbi["benefit"] = st.text_input("Para", pbi["benefit"], key=f"ben_{idx}")

    st.markdown("**✅ Happy Path**")
    for i, ac in enumerate(pbi.get("happy_path", [])):
        pbi["happy_path"][i] = st.text_input(f"AC{i+1}", ac, key=f"hp_{idx}_{i}", label_visibility="collapsed")

    if pbi.get("validations"):
        st.markdown("**⚠️ Validaciones**")
        for i, v in enumerate(pbi["validations"]):
            pbi["validations"][i] = st.text_input(f"V{i+1}", v, key=f"v_{idx}_{i}", label_visibility="collapsed")

    if pbi.get("error_states"):
        st.markdown("**🚨 Estados de Error**")
        for i, e in enumerate(pbi["error_states"]):
            pbi["error_states"][i] = st.text_input(f"E{i+1}", e, key=f"e_{idx}_{i}", label_visibility="collapsed")

    if pbi.get("prototype_refs"):
        st.markdown("**🖼️ Prototipo**")
        for i, r in enumerate(pbi["prototype_refs"]):
            pbi["prototype_refs"][i] = st.text_input(f"P{i+1}", r, key=f"pr_{idx}_{i}", label_visibility="collapsed")

    if pbi.get("tech_notes"):
        st.markdown("**💡 Notas Técnicas**")
        for i, n in enumerate(pbi["tech_notes"]):
            pbi["tech_notes"][i] = st.text_input(f"N{i+1}", n, key=f"tn_{idx}_{i}", label_visibility="collapsed")


# ========== MAIN UI ==========

# Custom CSS for Endalia branding
st.markdown("""
<style>
    /* Header bar */
    .endalia-header {
        background: linear-gradient(135deg, #1A56DB, #2563EB);
        padding: 24px 32px;
        border-radius: 12px;
        margin-bottom: 24px;
        display: flex;
        align-items: center;
        gap: 16px;
    }
    .endalia-header svg {
        width: 40px;
        height: 40px;
    }
    .endalia-header h1 {
        color: white !important;
        font-size: 24px !important;
        font-weight: 700 !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    .endalia-header p {
        color: rgba(255,255,255,0.8);
        font-size: 14px;
        margin: 4px 0 0 0;
    }
    
    /* Primary buttons */
    .stButton > button[kind="primary"] {
        background-color: #1A56DB !important;
        border-color: #1A56DB !important;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #1E40AF !important;
        border-color: #1E40AF !important;
    }
    
    /* Expander headers */
    .streamlit-expanderHeader {
        font-weight: 600 !important;
    }
    
    /* Info boxes */
    .stAlert [data-testid="stAlertContentInfo"] {
        border-left-color: #1A56DB;
    }
</style>

<div class="endalia-header">
    <div>
        <svg viewBox="0 0 40 40" fill="none">
            <rect width="40" height="40" rx="10" fill="white" fill-opacity="0.15"/>
            <path d="M20 8C13.37 8 8 13.37 8 20s5.37 12 12 12 12-5.37 12-12S26.63 8 20 8zm-2 17.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5zm6-7c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z" fill="white"/>
        </svg>
    </div>
    <div>
        <h1>📋 Generador de PBIs</h1>
        <p>Describe la funcionalidad → genera, edita y copia PBIs para Azure DevOps</p>
    </div>
</div>
""", unsafe_allow_html=True)

with st.container(border=True):
    col1, col2 = st.columns(2)
    with col1:
        module = st.text_input("Módulo", placeholder="Ej: Holidays & Absences")
    with col2:
        feature = st.text_input("Feature", placeholder="Ej: Políticas de V&A")

    description = st.text_area(
        "Descripción funcional *",
        placeholder="Desde algo breve ('quitar validación de suma, cada campo 0-100') hasta una feature completa...",
        height=150,
        key="desc_input"
    )

    # Speech-to-text component
    speech_html = """
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <button id="micBtn" onclick="toggleRecording()" style="background:#f1f5f9;color:#64748b;border:1px solid #d1d5db;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px;font-weight:500;display:inline-flex;align-items:center;gap:6px;">
                <span id="micIcon">🎤</span> <span id="micText">Dictar con voz</span>
            </button>
            <span id="micStatus" style="font-size:12px;color:#ef4444;display:none;">🔴 Grabando...</span>
            <button id="copyBtn" onclick="copyText()" style="background:#6366f1;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px;font-weight:500;display:none;">📋 Copiar texto</button>
            <button id="clearBtn" onclick="clearText()" style="background:#f1f5f9;color:#64748b;border:1px solid #d1d5db;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px;display:none;">Limpiar</button>
            <span id="copyStatus" style="font-size:12px;color:#10b981;display:none;">✓ Copiado, pégalo arriba</span>
        </div>
        <div id="transcriptBox" style="display:none;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:14px;color:#334155;line-height:1.6;min-height:40px;">
            <span id="transcriptText" style="color:#94a3b8;font-style:italic;">Esperando dictado...</span>
        </div>
    </div>
    <script>
    let recognition = null;
    let isRecording = false;
    let fullText = "";

    function toggleRecording() {
        if (isRecording) stopRecording();
        else startRecording();
    }

    function startRecording() {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) { alert("Tu navegador no soporta dictado por voz. Usa Chrome o Edge."); return; }
        recognition = new SR();
        recognition.lang = "es-ES";
        recognition.continuous = true;
        recognition.interimResults = true;

        recognition.onresult = (e) => {
            let interim = "";
            let newFinal = "";
            for (let i = e.resultIndex; i < e.results.length; i++) {
                const t = e.results[i][0].transcript;
                if (e.results[i].isFinal) newFinal += t + " ";
                else interim = t;
            }
            if (newFinal) fullText += newFinal;
            document.getElementById("transcriptText").innerHTML = fullText + (interim ? '<span style="color:#94a3b8">' + interim + '</span>' : '');
            document.getElementById("transcriptText").style.color = "#334155";
            document.getElementById("transcriptText").style.fontStyle = "normal";
        };

        recognition.onerror = () => stopRecording();
        recognition.onend = () => { if (isRecording) { try { recognition.start(); } catch(e) { stopRecording(); } } };

        recognition.start();
        isRecording = true;
        document.getElementById("transcriptBox").style.display = "block";
        document.getElementById("transcriptText").innerHTML = '<span style="color:#94a3b8;font-style:italic;">Escuchando...</span>';
        updateUI(true);
    }

    function stopRecording() {
        isRecording = false;
        if (recognition) { try { recognition.stop(); } catch(e) {} }
        updateUI(false);
        if (fullText.trim()) {
            document.getElementById("copyBtn").style.display = "inline-flex";
            document.getElementById("clearBtn").style.display = "inline-flex";
        }
    }

    async function copyText() {
        const text = fullText.trim();
        if (!text) return;
        try { await navigator.clipboard.writeText(text); }
        catch(e) {
            const ta = document.createElement("textarea");
            ta.value = text; ta.style.cssText = "position:fixed;opacity:0";
            document.body.appendChild(ta); ta.select();
            document.execCommand("copy"); document.body.removeChild(ta);
        }
        const s = document.getElementById("copyStatus");
        s.style.display = "inline";
        setTimeout(() => s.style.display = "none", 3000);
    }

    function clearText() {
        fullText = "";
        document.getElementById("transcriptText").innerHTML = '<span style="color:#94a3b8;font-style:italic;">Esperando dictado...</span>';
        document.getElementById("copyBtn").style.display = "none";
        document.getElementById("clearBtn").style.display = "none";
        document.getElementById("transcriptBox").style.display = "none";
    }

    function updateUI(recording) {
        const btn = document.getElementById("micBtn");
        const icon = document.getElementById("micIcon");
        const text = document.getElementById("micText");
        const status = document.getElementById("micStatus");
        if (recording) {
            btn.style.background = "#ef4444"; btn.style.color = "#fff"; btn.style.borderColor = "#ef4444";
            icon.textContent = "⏹"; text.textContent = "Parar";
            status.style.display = "inline";
        } else {
            btn.style.background = "#f1f5f9"; btn.style.color = "#64748b"; btn.style.borderColor = "#d1d5db";
            icon.textContent = "🎤"; text.textContent = "Dictar con voz";
            status.style.display = "none";
        }
    }
    </script>
    """
    st.components.v1.html(speech_html, height=130)

    context = st.text_area(
        "Contexto técnico (opcional)",
        placeholder="Endpoints, dependencias, restricciones...",
        height=80
    )

    # Prototype section
    st.markdown("---")
    st.markdown("**🎨 Prototipo**")

    figma_available = "FIGMA_TOKEN" in st.secrets

    tab_figma, tab_upload = st.tabs(["🔗 Enlace de Figma", "📁 Subir capturas"])

    with tab_figma:
        if figma_available:
            figma_url = st.text_input(
                "URL del prototipo de Figma",
                placeholder="https://www.figma.com/proto/... o https://www.figma.com/design/...",
                key="figma_url"
            )

            if figma_url:
                file_key, node_ids = parse_figma_url(figma_url)

                if file_key:
                    st.success(f"✅ Archivo detectado")

                    if st.button("📸 Capturar pantalla de Figma"):
                        with st.spinner("Exportando desde Figma..."):
                            export_ids = node_ids if node_ids else []

                            if not export_ids:
                                st.warning("No se detectó un nodo específico en la URL. Abre la pantalla concreta en Figma y copia la URL desde ahí.")
                            else:
                                figma_images = get_figma_images(file_key, export_ids, st.secrets["FIGMA_TOKEN"])

                                if figma_images:
                                    st.session_state["figma_images"] = figma_images
                                    st.success(f"✅ {len(figma_images)} captura(s) exportada(s)")
                                else:
                                    st.error("No se pudo exportar la imagen. Verifica que el token tiene acceso al archivo.")

                    # Show exported images
                    if "figma_images" in st.session_state and st.session_state["figma_images"]:
                        st.markdown("**Capturas exportadas:**")
                        for i, img in enumerate(st.session_state["figma_images"]):
                            img_bytes = base64.b64decode(img["data"])
                            st.image(img_bytes, caption=f"Captura {i+1}", use_container_width=True)

                        extra_nodes = st.text_input(
                            "¿Más pantallas? Pega otra URL de Figma",
                            placeholder="https://www.figma.com/design/...?node-id=XXXX-YYYY",
                            key="extra_figma"
                        )
                        if extra_nodes and st.button("➕ Añadir captura"):
                            extra_key, extra_ids = parse_figma_url(extra_nodes)
                            if extra_key and extra_ids:
                                with st.spinner("Exportando..."):
                                    extra_images = get_figma_images(extra_key, extra_ids, st.secrets["FIGMA_TOKEN"])
                                    if extra_images:
                                        st.session_state["figma_images"].extend(extra_images)
                                        st.success(f"✅ Añadida(s) {len(extra_images)} captura(s)")
                                        st.rerun()
                else:
                    st.error("URL no válida. Usa una URL de Figma (proto, design o file).")
        else:
            st.info("Para conectar con Figma, añade `FIGMA_TOKEN` en los Secrets de tu app.")

    with tab_upload:
        uploaded_files = st.file_uploader(
            "Sube capturas manualmente",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            help="Capturas de Figma o cualquier imagen del prototipo"
        )
        if uploaded_files:
            cols = st.columns(min(len(uploaded_files), 5))
            for i, f in enumerate(uploaded_files):
                with cols[i % 5]:
                    st.image(f, caption=f"Captura {i+1}", width=120)

    # Generate button
    generate_btn = st.button("🚀 Generar PBIs", type="primary", use_container_width=True)


# ========== PROCESS ==========

if generate_btn:
    if not description.strip():
        st.error("Añade una descripción funcional")
    else:
        all_images = []

        if "figma_images" in st.session_state:
            for img in st.session_state["figma_images"]:
                all_images.append({"data": img["data"], "media_type": img["media_type"]})

        if uploaded_files:
            for f in uploaded_files:
                b64 = base64.b64encode(f.read()).decode("utf-8")
                all_images.append({"data": b64, "media_type": f.type or "image/png"})

        with st.spinner("Analizando y generando PBIs..."):
            try:
                result = generate_pbis(module, feature, description, context, all_images)
                st.session_state["result"] = result
            except Exception as e:
                st.error(f"Error al generar: {e}")


# ========== DISPLAY RESULTS ==========

if "result" in st.session_state:
    result = st.session_state["result"]

    st.markdown(f"## PBIs Generados ({len(result['pbis'])})")

    if result.get("summary"):
        st.info(f"💡 **Análisis de división:** {result['summary']}")

    for i, pbi in enumerate(result["pbis"]):
        with st.expander(f"US {i+1}/{len(result['pbis'])} — {pbi['title']}", expanded=True):
            render_pbi_card(pbi, i, len(result["pbis"]))
