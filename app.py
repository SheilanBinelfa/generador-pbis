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
  * Happy Path: cada AC debe describir un comportamiento verificable.
  * Validaciones: reglas de negocio, límites, formatos, estados no permitidos.
  * Errores: comportamiento ante fallos de red, datos vacíos, timeouts — solo si son relevantes.
  * Si una funcionalidad tiene múltiples columnas, campos o comportamientos, DETALLA cada uno.
- Prototipo: "(Captura X) Muestra [descripción detallada]"
- Dependencias: entre PBIs si los hay
- Notas Técnicas: preguntas concretas para desarrollo

RESPONDE SOLO JSON válido sin backticks:
{
  "summary": "Justificación de la división",
  "pbis": [{
    "title": "...", "objective": "...", "role": "...", "when": "...", "then": "...", "benefit": "...",
    "happy_path": ["AC1: ..."], "validations": ["AC-V1: ..."], "error_states": ["AC-E1: ..."],
    "prototype_refs": ["(Captura 1) Muestra..."], "dependencies": [], "tech_notes": ["..."]
  }]
}"""


# ========== AZURE DEVOPS ==========

def get_azure_connection():
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication
    credentials = BasicAuthentication("", st.secrets["AZURE_PAT"])
    return Connection(base_url=f"https://dev.azure.com/{st.secrets['AZURE_ORG']}", creds=credentials)


def upload_image_to_azure(wit_client, image_b64, filename, project):
    import io
    image_bytes = base64.b64decode(image_b64)
    stream = io.BytesIO(image_bytes)
    attachment = wit_client.create_attachment(upload_stream=stream, file_name=filename, project=project)
    return attachment.url


def push_pbi_to_azure(pbi, iteration_path=None, area_path=None, parent_id=None, figma_b64=None, figma_link=None, existing_id=None, endalia_module=None, microservice=None, value_area=None):
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation
    conn = get_azure_connection()
    wit_client = conn.clients.get_work_item_tracking_client()
    project = st.secrets["AZURE_PROJECT"]

    attachment_urls = []
    if figma_b64:
        for i, b64 in enumerate(figma_b64):
            if b64:
                try:
                    url = upload_image_to_azure(wit_client, b64, f"captura_{i+1}.png", project)
                    attachment_urls.append(url)
                except Exception as e:
                    st.warning(f"No se pudo subir Captura {i+1}: {e}")
                    attachment_urls.append(None)
            else:
                attachment_urls.append(None)

    html_desc = pbi_to_html_with_urls(pbi, attachment_urls, figma_link)

    if existing_id:
        patch = [
            {"op": "replace", "path": "/fields/System.Title", "value": pbi["title"]},
            {"op": "replace", "path": "/fields/System.Description", "value": html_desc},
        ]
        if iteration_path:
            patch.append({"op": "replace", "path": "/fields/System.IterationPath", "value": iteration_path})
        if area_path:
            patch.append({"op": "replace", "path": "/fields/System.AreaPath", "value": area_path})
        patch_ops = [JsonPatchOperation(**p) for p in patch]
        return wit_client.update_work_item(document=patch_ops, id=existing_id, project=project)
    else:
        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": pbi["title"]},
            {"op": "add", "path": "/fields/System.Description", "value": html_desc},
        ]
        if iteration_path:
            patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
        if area_path:
            patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
        if endalia_module:
            patch.append({"op": "add", "path": "/fields/Custom.EndaliaModule", "value": endalia_module})
        if microservice:
            patch.append({"op": "add", "path": "/fields/Custom.MicroserviceVersion", "value": microservice})
        if value_area:
            patch.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.ValueArea", "value": value_area})
        if parent_id:
            patch.append({"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"https://dev.azure.com/{st.secrets['AZURE_ORG']}/_apis/wit/workItems/{parent_id}",
            }})
        patch_ops = [JsonPatchOperation(**p) for p in patch]
        return wit_client.create_work_item(document=patch_ops, project=project, type="Product Backlog Item")


def create_child_tasks(wit_client, project, pbi_id, task_titles, iteration_path=None, area_path=None):
    """Create Task work items as children of the given PBI, one per title in task_titles."""
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation
    org = st.secrets["AZURE_ORG"]
    created = []
    for title in task_titles:
        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": title or "Task"},
            {"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"https://dev.azure.com/{org}/_apis/wit/workItems/{pbi_id}",
            }},
        ]
        if iteration_path:
            patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
        if area_path:
            patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
        patch_ops = [JsonPatchOperation(**p) for p in patch]
        task = wit_client.create_work_item(document=patch_ops, project=project, type="Task")
        created.append(task.id)
    return created


# ========== FIGMA ==========

def parse_figma_url(url):
    url = url.strip()
    proto_match = re.search(r'figma\.com/proto/([a-zA-Z0-9]+)', url)
    design_match = re.search(r'figma\.com/(?:design|file)/([a-zA-Z0-9]+)', url)
    file_key = proto_match.group(1) if proto_match else (design_match.group(1) if design_match else None)
    if not file_key:
        return None, None
    node_ids = set()
    for param in ['node-id', 'starting-point-node-id']:
        if param in url:
            nid = re.search(rf'{param}=([^&]+)', url)
            if nid:
                node_ids.add(nid.group(1).replace('-', ':'))
    return file_key, list(node_ids)


def get_figma_images(file_key, node_ids, figma_token):
    headers = {"X-Figma-Token": figma_token}
    ids_str = ",".join(node_ids)
    resp = requests.get(f"https://api.figma.com/v1/images/{file_key}?ids={ids_str}&format=png&scale=2", headers=headers)
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
                images.append({"data": b64, "media_type": "image/png", "node_id": node_id, "url": img_url})
    return images


# ========== HTML FORMATTING ==========

def _build_pbi_html_body(p):
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
    return h


def pbi_to_html(p, figma_images_b64=None, figma_link=None):
    h = _build_pbi_html_body(p)
    h += "<h3>🖼️ Prototipo</h3>"
    if figma_link:
        h += f'<p><a href="{figma_link}">Ver prototipo en Figma</a></p>'
    if p.get("prototype_refs"):
        for r in p["prototype_refs"]:
            h += f"<p><b>{r}</b></p>"
            cap_match = re.search(r'[Cc]aptura\s*(\d+)', r)
            if cap_match and figma_images_b64:
                cap_idx = int(cap_match.group(1)) - 1
                if 0 <= cap_idx < len(figma_images_b64):
                    h += f'<p><img src="data:image/png;base64,{figma_images_b64[cap_idx]}" style="max-width:800px;border:1px solid #ddd;border-radius:4px;" /></p>'
    elif figma_images_b64:
        for i, b64 in enumerate(figma_images_b64):
            h += f"<p><b>({i+1})</b></p><p><img src=\"data:image/png;base64,{b64}\" style=\"max-width:800px;\" /></p>"
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


def pbi_to_html_with_urls(p, attachment_urls=None, figma_link=None):
    h = _build_pbi_html_body(p)
    h += "<h3>🖼️ Prototipo</h3>"
    if figma_link:
        h += f'<p><a href="{figma_link}">Ver prototipo en Figma</a></p>'
    if p.get("prototype_refs"):
        for r in p["prototype_refs"]:
            h += f"<p><b>{r}</b></p>"
            cap_match = re.search(r'[Cc]aptura\s*(\d+)', r)
            if cap_match and attachment_urls:
                cap_idx = int(cap_match.group(1)) - 1
                if 0 <= cap_idx < len(attachment_urls) and attachment_urls[cap_idx]:
                    h += f'<p><img src="{attachment_urls[cap_idx]}" style="max-width:800px;" /></p>'
    elif attachment_urls:
        for i, url in enumerate(attachment_urls):
            if url:
                h += f"<p><b>({i+1})</b></p><p><img src=\"{url}\" style=\"max-width:800px;\" /></p>"
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


# ========== GENERATION ==========

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
        user_content.append({"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}})
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


# ========== PBI CARD ==========

def render_pbi_card(pbi, idx, total, default_iteration="", default_area="", default_module="", default_microservice="", default_value_area=""):
    figma_b64 = []
    figma_link = st.session_state.get("figma_url", None)
    if "figma_images" in st.session_state:
        figma_b64 = [img.get("data", "") for img in st.session_state["figma_images"]]
    if "uploaded_b64" in st.session_state:
        figma_b64.extend(st.session_state["uploaded_b64"])

    html_content = pbi_to_html(pbi, figma_b64, figma_link)

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
                await navigator.clipboard.write([new ClipboardItem({{
                    "text/html": new Blob([html], {{type:"text/html"}}),
                    "text/plain": new Blob([plain], {{type:"text/plain"}})
                }})]);
            }} catch(e) {{
                const div = document.createElement("div");
                div.innerHTML = html; div.style.cssText = "position:fixed;left:-9999px;opacity:0";
                document.body.appendChild(div);
                const range = document.createRange(); range.selectNodeContents(div);
                const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
                document.execCommand("copy"); sel.removeAllRanges(); document.body.removeChild(div);
            }}
            const s = document.getElementById("status_{idx}");
            s.style.display = "inline"; setTimeout(() => s.style.display = "none", 2000);
        }}
        </script>""", height=50)

    azure_available = all(k in st.secrets for k in ["AZURE_PAT", "AZURE_ORG", "AZURE_PROJECT"])
    with col_push:
        if azure_available:
            with st.popover("🚀 Push to Azure", use_container_width=True):
                st.markdown(f"**`{pbi['title']}`**")
                mode = st.radio("Acción", ["Crear nuevo PBI", "Actualizar PBI existente"], key=f"mode_{idx}", horizontal=True)
                existing_id = None
                if mode == "Actualizar PBI existente":
                    existing_id_str = st.text_input("ID del Work Item", placeholder="Ej: 203734", key=f"existing_{idx}")
                    if existing_id_str:
                        existing_id = int(existing_id_str)
                iteration = st.text_input("Iteration Path", value=default_iteration, key=f"iter_{idx}")
                area = st.text_input("Area Path", value=default_area, key=f"area_{idx}")

                st.markdown("---")
                mc1, mc2 = st.columns(2)
                with mc1:
                    endalia_module = st.selectbox("Endalia Module", ["Registro y planificación horaria", "Vacaciones y ausencias"], key=f"emodule_{idx}")
                    value_area = st.selectbox("Value Area", ["Product improvement", "Roadmap", "Operations improvement"], key=f"varea_{idx}")
                with mc2:
                    microservice = st.selectbox("Microservice Version", ["Candidate", "Candidate+1"], key=f"msvc_{idx}")

                st.markdown("---")
                parent = ""
                if mode == "Crear nuevo PBI":
                    parent = st.text_input("Parent Feature ID (opcional)", placeholder="Ej: 177040", key=f"parent_{idx}")

                # ---- Task creation ----
                if mode == "Crear nuevo PBI":
                    st.markdown("---")
                    create_tasks = st.checkbox("Crear task(s) hija(s) automáticamente", key=f"create_tasks_{idx}")
                    num_tasks = 1
                    task_titles = []
                    if create_tasks:
                        num_tasks = st.number_input(
                            "¿Cuántas tasks?",
                            min_value=1, max_value=10, value=1, step=1,
                            key=f"num_tasks_{idx}",
                            help="Cada task quedará vinculada como hija (child) del PBI."
                        )
                        st.markdown("**Títulos de las tasks** *(edítalos si quieres)*")
                        for t in range(int(num_tasks)):
                            title = st.text_input(
                                f"Task {t+1}",
                                value=pbi["title"],
                                key=f"task_title_{idx}_{t}",
                                label_visibility="collapsed",
                                placeholder=f"Título task {t+1}"
                            )
                            task_titles.append(title)

                btn_label = "✅ Actualizar PBI" if existing_id else "✅ Crear PBI"
                if st.button(btn_label, key=f"push_{idx}", type="primary", use_container_width=True):
                    with st.spinner("Enviando a Azure DevOps..."):
                        try:
                            parent_id = int(parent) if parent and parent.strip() else None
                            result = push_pbi_to_azure(pbi, iteration_path=iteration if iteration.strip() else None,
                                area_path=area if area.strip() else None, parent_id=parent_id,
                                figma_b64=figma_b64, figma_link=figma_link, existing_id=existing_id,
                                endalia_module=endalia_module, microservice=microservice, value_area=value_area)
                            action = "actualizado" if existing_id else "creado"
                            pbi_url = f"https://dev.azure.com/{st.secrets['AZURE_ORG']}/{st.secrets['AZURE_PROJECT']}/_workitems/edit/{result.id}"
                            st.success(f"✅ PBI {action} — ID: **{result.id}** — [Abrir en Azure]({pbi_url})")

                            # Create child tasks if requested
                            if mode == "Crear nuevo PBI" and create_tasks and num_tasks > 0:
                                with st.spinner(f"Creando {int(num_tasks)} task(s) hija(s)..."):
                                    conn = get_azure_connection()
                                    wit_client = conn.clients.get_work_item_tracking_client()
                                    task_ids = create_child_tasks(
                                        wit_client,
                                        project=st.secrets["AZURE_PROJECT"],
                                        pbi_id=result.id,
                                        task_titles=task_titles,
                                        iteration_path=iteration if iteration.strip() else None,
                                        area_path=area if area.strip() else None,
                                    )
                                    ids_str = ", ".join(f"**{t}**" for t in task_ids)
                                    st.success(f"✅ {len(task_ids)} task(s) creada(s) — IDs: {ids_str}")
                        except Exception as e:
                            st.error(f"Error: {e}")

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
    if pbi.get("prototype_refs") or "figma_images" in st.session_state or "uploaded_b64" in st.session_state:
        st.markdown("**🖼️ Prototipo**")
        # Show figma link
        figma_link_val = st.session_state.get("figma_url", "")
        if figma_link_val:
            st.markdown(f"[🔗 Ver prototipo en Figma]({figma_link_val})")
        # Show uploaded images inline
        all_imgs = []
        if "figma_images" in st.session_state:
            all_imgs += [base64.b64decode(img["data"]) for img in st.session_state["figma_images"]]
        if "uploaded_b64" in st.session_state:
            all_imgs += [base64.b64decode(b) for b in st.session_state["uploaded_b64"]]
        if all_imgs:
            img_cols = st.columns(min(len(all_imgs), 3))
            for ci, img_bytes in enumerate(all_imgs):
                with img_cols[ci % 3]:
                    st.image(img_bytes, caption=f"Captura {ci+1}", use_container_width=True)
        if pbi.get("prototype_refs"):
            for i, r in enumerate(pbi["prototype_refs"]):
                pbi["prototype_refs"][i] = st.text_input(f"P{i+1}", r, key=f"pr_{idx}_{i}", label_visibility="collapsed")
    if pbi.get("tech_notes"):
        st.markdown("**💡 Notas Técnicas**")
        for i, n in enumerate(pbi["tech_notes"]):
            pbi["tech_notes"][i] = st.text_input(f"N{i+1}", n, key=f"tn_{idx}_{i}", label_visibility="collapsed")




# ========== MAIN UI ==========

from streamlit_mic_recorder import speech_to_text

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}

/* ── Header ── */
.pbi-header {
    background: #0f172a;
    border-bottom: 3px solid #2563EB;
    padding: 20px 32px;
    margin: -1rem -1rem 1.5rem -1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.pbi-header-left h1 {
    color: #f8fafc !important;
    font-size: 20px !important;
    font-weight: 700 !important;
    margin: 0 !important;
    letter-spacing: -0.3px;
}
.pbi-header-left p {
    color: #94a3b8;
    font-size: 13px;
    margin: 2px 0 0 0;
}
.pbi-badge {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 12px;
    color: #94a3b8;
    font-family: 'IBM Plex Mono', monospace;
    display: flex;
    gap: 16px;
}
.pbi-badge span { color: #f8fafc; font-weight: 600; }

/* ── Panels ── */
.panel-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #64748b;
    margin-bottom: 12px;
}

/* ── Defaults sprint ── */
.defaults-bar {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 16px;
}

/* ── PBI cards ── */
.pbi-card-header {
    background: #0f172a;
    color: white;
    padding: 10px 16px;
    border-radius: 8px 8px 0 0;
    font-size: 13px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.pbi-section {
    border-left: 3px solid #2563EB;
    padding: 8px 12px;
    margin: 8px 0;
    background: #f8fafc;
    border-radius: 0 6px 6px 0;
}
.pbi-section-ac {
    border-left: 3px solid #10b981;
    padding: 8px 12px;
    margin: 8px 0;
    background: #f0fdf4;
    border-radius: 0 6px 6px 0;
}
.pbi-section-val {
    border-left: 3px solid #f59e0b;
    padding: 8px 12px;
    margin: 8px 0;
    background: #fffbeb;
    border-radius: 0 6px 6px 0;
}
.pbi-section-err {
    border-left: 3px solid #ef4444;
    padding: 8px 12px;
    margin: 8px 0;
    background: #fef2f2;
    border-radius: 0 6px 6px 0;
}
.section-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .8px;
    margin-bottom: 6px;
}

/* ── Progress stepper ── */
.stepper {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    padding: 10px 0;
}
.step-done { color: #10b981; font-weight: 600; }
.step-arrow { color: #94a3b8; }

/* ── Generate button sticky ── */
.stButton > button[kind="primary"] {
    background: #2563EB !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 15px !important;
    padding: 12px !important;
    transition: background .15s !important;
}
.stButton > button[kind="primary"]:hover {
    background: #1d4ed8 !important;
}
</style>
""", unsafe_allow_html=True)

# ── Header con badge de estado ──
n_pbis = len(st.session_state.get("result", {}).get("pbis", []))
module_badge = st.session_state.get("_last_module", "—")
sprint_badge = st.session_state.get("default_iteration", "—")

st.markdown(f"""
<div class="pbi-header">
  <div class="pbi-header-left">
    <h1>📋 Generador de PBIs</h1>
    <p>Describe la funcionalidad → genera, edita y copia PBIs para Azure DevOps</p>
  </div>
  <div class="pbi-badge">
    PBIs: <span>{n_pbis}</span>
    &nbsp;·&nbsp; Módulo: <span>{module_badge}</span>
    &nbsp;·&nbsp; Sprint: <span>{sprint_badge}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Layout dos columnas: formulario | resultados ──
col_form, col_results = st.columns([5, 7], gap="large")

with col_form:
    st.markdown('<div class="panel-label">⚙️ Configuración del PBI</div>', unsafe_allow_html=True)

    # Defaults globales de sprint/área
    with st.container(border=True):
        st.markdown('<div class="panel-label" style="margin-bottom:8px">🗂️ Valores por defecto para Azure</div>', unsafe_allow_html=True)
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            default_iteration = st.text_input(
                "Iteration Path",
                key="default_iteration",
                value=st.session_state.get("default_iteration", "SWArea")
            )
            default_module = st.selectbox(
                "Endalia Module",
                ["Registro y planificación horaria", "Vacaciones y ausencias"],
                key="default_module"
            )
        with dcol2:
            default_area = st.text_input(
                "Area Path",
                key="default_area",
                value=st.session_state.get("default_area", "SWArea\\Product\\Core\\CoreProduct1")
            )
            dcol_ms, dcol_va = st.columns(2)
            with dcol_ms:
                default_microservice = st.selectbox("Microservice", ["Candidate", "Candidate+1"], key="default_microservice")
            with dcol_va:
                default_value_area = st.selectbox("Value Area", ["Product improvement", "Roadmap", "Operations improvement"], key="default_value_area")

        if st.button("🔄 Nuevo PBI — limpiar todo", use_container_width=True):
            for k in ["result", "desc_value", "figma_images", "uploaded_b64", "last_voice_text", "figma_url", "_last_module", "desc_input"]:
                st.session_state.pop(k, None)
            st.rerun()

    with st.container(border=True):
        c1, c2 = st.columns(2)
        with c1:
            module = st.text_input("Módulo", placeholder="Ej: Holidays & Absences")
        with c2:
            feature = st.text_input("Feature", placeholder="Ej: Políticas de V&A")

        description = st.text_area(
            "Descripción funcional *",
            placeholder="Desde algo breve ('quitar validación de suma, cada campo 0-100') hasta una feature completa...",
            height=150,
            key="desc_input"
        )

        with st.expander("🎤 Dictar con voz"):
            voice_text = speech_to_text(
                start_prompt="⏺️ Iniciar grabación",
                stop_prompt="⏹️ Parar grabación",
                language="es",
                use_container_width=True,
                key="voice_recorder"
            )
            if voice_text:
                st.session_state["last_voice_text"] = voice_text
            if st.session_state.get("last_voice_text"):
                st.markdown("**Texto dictado** — cópialo y pégalo arriba:")
                st.code(st.session_state["last_voice_text"], language=None)

        context = st.text_area(
            "Contexto técnico (opcional)",
            placeholder="Endpoints, dependencias, restricciones...",
            height=80
        )

        st.markdown("**🎨 Prototipo**")
        figma_available = "FIGMA_TOKEN" in st.secrets
        tab_figma, tab_upload = st.tabs(["🔗 Figma", "📁 Capturas"])

        with tab_figma:
            if figma_available:
                figma_url = st.text_input(
                    "URL del prototipo",
                    placeholder="https://www.figma.com/proto/...",
                    key="figma_url"
                )
                if figma_url:
                    file_key, node_ids = parse_figma_url(figma_url)
                    if file_key:
                        st.success("✅ Archivo detectado")
                        if st.button("📸 Exportar desde Figma"):
                            with st.spinner("Exportando..."):
                                if not node_ids:
                                    st.warning("No se detectó nodo en la URL.")
                                else:
                                    figma_images = get_figma_images(file_key, node_ids, st.secrets["FIGMA_TOKEN"])
                                    if figma_images:
                                        st.session_state["figma_images"] = figma_images
                                        st.success(f"✅ {len(figma_images)} captura(s)")
                                    else:
                                        st.error("No se pudo exportar.")
                        if "figma_images" in st.session_state and st.session_state["figma_images"]:
                            for i, img in enumerate(st.session_state["figma_images"]):
                                st.image(base64.b64decode(img["data"]), caption=f"Captura {i+1}", use_container_width=True)
                            extra_nodes = st.text_input("Añadir otra pantalla", key="extra_figma")
                            if extra_nodes and st.button("➕ Añadir"):
                                extra_key, extra_ids = parse_figma_url(extra_nodes)
                                if extra_key and extra_ids:
                                    with st.spinner("Exportando..."):
                                        extra_images = get_figma_images(extra_key, extra_ids, st.secrets["FIGMA_TOKEN"])
                                        if extra_images:
                                            st.session_state["figma_images"].extend(extra_images)
                                            st.rerun()
                    else:
                        st.error("URL no válida.")
            else:
                st.info("Añade `FIGMA_TOKEN` en Secrets para conectar con Figma.")

        with tab_upload:
            uploaded_files = st.file_uploader(
                "Sube capturas",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
            )
            if uploaded_files:
                cols = st.columns(min(len(uploaded_files), 4))
                for i, f in enumerate(uploaded_files):
                    with cols[i % 4]:
                        st.image(f, caption=f"Captura {i+1}", width=100)

        st.markdown("")
        generate_btn = st.button("🚀 Generar PBIs", type="primary", use_container_width=True)


# ========== PROCESS ==========

uploaded_files = uploaded_files if 'uploaded_files' in dir() else []

if generate_btn:
    description = st.session_state.get("desc_input", "")
    if not description.strip():
        with col_form:
            st.error("Añade una descripción funcional")
    else:
        st.session_state["_last_module"] = module or "—"
        all_images = []
        if "figma_images" in st.session_state:
            for img in st.session_state["figma_images"]:
                all_images.append({"data": img["data"], "media_type": img["media_type"]})
        if uploaded_files:
            uploaded_b64_list = []
            for f in uploaded_files:
                b64 = base64.b64encode(f.read()).decode("utf-8")
                all_images.append({"data": b64, "media_type": f.type or "image/png"})
                uploaded_b64_list.append(b64)
            st.session_state["uploaded_b64"] = uploaded_b64_list
        with col_results:
            with st.spinner("Analizando y generando PBIs..."):
                try:
                    result = generate_pbis(module, feature, description, context, all_images)
                    st.session_state["result"] = result
                except Exception as e:
                    st.error(f"Error al generar: {e}")


# ========== DISPLAY RESULTS ==========

with col_results:
    if "result" in st.session_state:
        result = st.session_state["result"]
        n = len(result["pbis"])

        # Header de resultados con badge
        st.markdown(f"""
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
            <div style="font-size:18px;font-weight:700;color:#0f172a;">
                PBIs generados
            </div>
            <div style="background:#2563EB;color:white;border-radius:20px;padding:4px 14px;font-size:13px;font-weight:600;">
                {n} PBI{'s' if n != 1 else ''}
            </div>
        </div>
        """, unsafe_allow_html=True)

        if result.get("summary"):
            st.info(f"💡 {result['summary']}")

        for i, pbi in enumerate(result["pbis"]):
            with st.expander(f"US {i+1}/{n} — {pbi['title']}", expanded=True):
                render_pbi_card(pbi, i, n,
                                default_iteration=st.session_state.get("default_iteration", ""),
                                default_area=st.session_state.get("default_area", ""),
                                default_module=st.session_state.get("default_module", "Registro y planificación horaria"),
                                default_microservice=st.session_state.get("default_microservice", "Candidate"),
                                default_value_area=st.session_state.get("default_value_area", "Product improvement"))
    else:
        st.markdown("""
        <div style="height:300px;display:flex;flex-direction:column;align-items:center;justify-content:center;
                    color:#94a3b8;border:2px dashed #e2e8f0;border-radius:12px;text-align:center;padding:32px;">
            <div style="font-size:40px;margin-bottom:12px;">📋</div>
            <div style="font-size:15px;font-weight:600;color:#64748b;">Aquí aparecerán los PBIs</div>
            <div style="font-size:13px;margin-top:6px;">Rellena el formulario y pulsa Generar</div>
        </div>
        """, unsafe_allow_html=True)
