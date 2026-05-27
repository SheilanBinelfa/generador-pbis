import streamlit as st
import anthropic
import json
import base64
import requests
import re

st.set_page_config(page_title="Generador de PBIs", page_icon="📋", layout="wide")

SYSTEM_PROMPT = """Eres un asistente experto en Product Management que genera Product Backlog Items (PBIs) completos y prescriptivos para Azure DevOps.
Tu audiencia son desarrolladores y QA que deben poder implementar y testear sin necesidad de preguntar al PM.

---

## EL INPUT DEL USUARIO PUEDE SER

- **Texto breve e informal**: estructura y completa la información.
- **Descripción larga de una feature**: propón la división óptima en PBIs.
- **Capturas de pantalla o prototipo**: analízalas en detalle antes de escribir.

---

## FASE 1 — ANALIZAR EL PROTOTIPO (si hay capturas)

Antes de escribir el PBI, analiza exhaustivamente cada captura aportada:

1. Identifica todos los elementos visuales: títulos, subtítulos, textos descriptivos, etiquetas de campos, placeholders, botones, chips, banners y mensajes.
2. Copia los textos literales exactos tal como aparecen en pantalla. No parafrasees ni resumas.
3. Clasifica cada control interactivo: tipo, si tiene prefijo/sufijo, las opciones disponibles, el valor por defecto y si es obligatorio.
4. Identifica comportamientos condicionales: qué aparece, desaparece o cambia al activar un control.
5. Identifica banners y mensajes de error: su tipo (info / warning / error) y la condición que los dispara.
6. Detecta estados especiales: opciones deshabilitadas, campos de solo lectura, estados vacíos, chips de estado.
7. Señala lo que no puedes ver: si hay estados alternativos que las capturas no cubren, indícalo en tech_notes.

---

## FASE 2 — DETECTAR DISCREPANCIAS (si hay descripción de la feature)

Antes de redactar, compara la descripción con las capturas e identifica:
- Errores críticos: lo que la descripción contradice directamente el prototipo.
- Incoherencias de diseño: elementos del prototipo que la descripción indica que no deben desarrollarse.
- Omisiones: secciones, campos, estados o comportamientos presentes en el prototipo no mencionados.
- Errores tipográficos: corrígelos en el PBI.

---

## REGLAS DE DIVISIÓN EN PBIs

- Evalúa la complejidad real. Un cambio de validación puntual = 1 PBI.
- Solo divide cuando hay flujos claramente independientes.
- Justifica la decisión en el campo "summary".

---

## ESPECIFICACIÓN FUNCIONAL

La sección "functional_spec" describe exactamente qué debe haber en pantalla: textos literales, etiquetas, opciones, comportamientos condicionales y estados. Es la fuente de verdad para el desarrollador. No usar lenguaje ambiguo.

Para cada elemento especifica:
- Textos literales: etiquetas, títulos, descripciones, placeholders y mensajes tal como aparecen.
- Controles de formulario: tipo, sufijo/prefijo, opciones, valor por defecto y si es obligatorio.
- Banners y alertas: tipo (info/warning/error), texto literal y condición de aparición.
- Comportamientos condicionales: qué aparece, desaparece o cambia al interactuar.
- Comportamientos automáticos: qué se recalcula o actualiza al cambiar un valor.

Usa comillas simples para nombres de campos/secciones y comillas dobles para textos literales en pantalla.

---

## CRITERIOS DE ACEPTACIÓN

Cada criterio es una acción concreta seguida de un resultado observable y verificable.
Cubre: carga inicial, campos obligatorios, cada comportamiento condicional, cada validación, actualizaciones automáticas y estados especiales.

---

RESPONDE SOLO JSON válido sin backticks ni markdown:
{
  "summary": "Justificación de la división y análisis de discrepancias si las hay",
  "pbis": [{
    "title": "...",
    "objective": "...",
    "role": "...",
    "when": "...",
    "then": "...",
    "benefit": "...",
    "functional_spec": "Especificación funcional completa en texto plano con saltos de línea",
    "happy_path": ["AC1: acción → resultado verificable"],
    "validations": ["AC-V1: acción → resultado verificable"],
    "error_states": ["AC-E1: acción → resultado verificable"],
    "prototype_refs": ["(Captura 1) Descripción exhaustiva con textos literales"],
    "dependencies": [],
    "tech_notes": ["Pregunta o aclaración concreta para desarrollo"]
  }]
}"""


# ========== AZURE DEVOPS ==========

def get_azure_connection():
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication
    pat = st.session_state.get("user_pat") or st.secrets.get("AZURE_PAT", "")
    org = st.session_state.get("user_org") or st.secrets.get("AZURE_ORG", "")
    credentials = BasicAuthentication("", pat)
    return Connection(base_url=f"https://dev.azure.com/{org}", creds=credentials)

def get_org():
    return st.session_state.get("user_org") or st.secrets.get("AZURE_ORG", "")

def get_project():
    return st.session_state.get("user_project") or st.secrets.get("AZURE_PROJECT", "")

@st.cache_data(show_spinner=False, ttl=300)
def fetch_iterations(pat, org, project, team="CoreProduct1"):
    """Fetch sprint iterations under PRODUCT from Azure DevOps team settings."""
    try:
        # Get team iterations (only the ones assigned to this team)
        for team_name in [team, f"{team} Team"]:
            team_enc = requests.utils.quote(team_name)
            url = f"https://dev.azure.com/{org}/{project}/{team_enc}/_apis/work/teamsettings/iterations?api-version=7.1"
            resp = requests.get(url, auth=("", pat), timeout=10)
            if resp.status_code == 200:
                iterations = resp.json().get("value", [])
                result = []
                for it in iterations:
                    path = it.get("path", "")
                    name = it.get("name", "")
                    # Only include PRODUCT sprints (not LEGACY or root)
                    if "PRODUCT" in path:
                        # Show only from PRODUCT onward for readability
                        short = path.split("PRODUCT")[-1].lstrip("\\")
                        label = f"PRODUCT\\{short}" if short else name
                        result.append({
                            "label": label,
                            "path": path,
                            "name": name,
                            "id": it.get("id", "")
                        })
                if result:
                    return result
        return []
    except Exception:
        return []

@st.cache_data(show_spinner=False, ttl=300)
def fetch_area_paths(pat, org, project):
    """Fetch only SWArea\\Product\\Core\\CoreProductN paths."""
    try:
        url = f"https://dev.azure.com/{org}/{project}/_apis/wit/classificationnodes/areas?$depth=10&api-version=7.1"
        resp = requests.get(url, auth=("", pat), timeout=10)
        if resp.status_code != 200:
            return []

        def find_node(node, name):
            if node.get("name") == name:
                return node
            for child in node.get("children", []):
                result = find_node(child, name)
                if result:
                    return result
            return None

        root = resp.json()
        # Navigate: root -> SWArea -> Product -> Core -> CoreProductN
        swarea = find_node(root, "SWArea") or root
        product = find_node(swarea, "Product")
        core = find_node(product, "Core") if product else None
        if core:
            import re as _re
            paths = []
            for child in core.get("children", []):
                name = child.get("name", "")
                if _re.match(r"CoreProduct\d+$", name):
                    paths.append(f"SWArea\\Product\\Core\\{name}")
            return sorted(paths)
        return []
    except Exception:
        return []

@st.cache_data(show_spinner=False, ttl=300)
def fetch_modules(pat, org, project):
    """Fetch Endalia Module allowed values via picklist API."""
    try:
        # Step 1: get the field definition to find the picklist ID
        url1 = f"https://dev.azure.com/{org}/_apis/work/processes/fields?api-version=7.1-preview.2"
        resp1 = requests.get(url1, auth=("", pat), timeout=10)
        if resp1.status_code == 200:
            for f in resp1.json().get("value", []):
                ref = f.get("referenceName", "")
                if "EndaliaModule" in ref or ("Module" in f.get("name","") and "Custom" in ref):
                    picklist_id = f.get("picklistId") or (f.get("picklist") or {}).get("id")
                    if picklist_id:
                        url2 = f"https://dev.azure.com/{org}/_apis/work/processes/lists/{picklist_id}?api-version=7.1-preview.1"
                        resp2 = requests.get(url2, auth=("", pat), timeout=10)
                        if resp2.status_code == 200:
                            items = resp2.json().get("items", [])
                            values = [i.get("value","") for i in items if i.get("value")]
                            if values:
                                return sorted(values)
        # Step 2: fallback — get allowed values from work item type fields
        url3 = f"https://dev.azure.com/{org}/{project}/_apis/wit/workitemtypes/Product%20Backlog%20Item/fields?$expand=allowedValues&api-version=7.1"
        resp3 = requests.get(url3, auth=("", pat), timeout=10)
        if resp3.status_code == 200:
            for f in resp3.json().get("value", []):
                if "EndaliaModule" in f.get("referenceName","") or "Module" in f.get("name",""):
                    vals = f.get("allowedValues", [])
                    if vals:
                        return sorted(vals)
        return []
    except Exception:
        return []

@st.cache_data(show_spinner=False, ttl=300)
def fetch_sprint_members(pat, org, project, team, iteration_path):
    """Fetch capacity members. Uses current sprint if iteration_path matches, else searches all."""
    try:
        for team_name in [team, team + " Team"]:
            team_enc = requests.utils.quote(team_name)
            base = f"https://dev.azure.com/{org}/{project}/{team_enc}/_apis/work/teamsettings/iterations"

            # Try current sprint first (fastest)
            resp_cur = requests.get(base + "?$timeframe=current&api-version=7.1",
                                    auth=("", pat), timeout=10)
            iter_id = None
            if resp_cur.status_code == 200:
                cur_iters = resp_cur.json().get("value", [])
                if cur_iters:
                    cur = cur_iters[0]
                    # Check if this is the selected sprint
                    cur_name = cur.get("name", "")
                    last_seg = iteration_path.split(chr(92))[-1]
                    if cur_name == last_seg or last_seg in cur.get("path", ""):
                        iter_id = cur["id"]

            # If not current, search all iterations
            if not iter_id:
                resp_all = requests.get(base + "?api-version=7.1",
                                        auth=("", pat), timeout=10)
                if resp_all.status_code == 200:
                    last_seg = iteration_path.split(chr(92))[-1]
                    for it in resp_all.json().get("value", []):
                        if it.get("name", "") == last_seg:
                            iter_id = it["id"]
                            break

            if not iter_id:
                continue

            # Fetch capacities
            url_cap = f"{base}/{iter_id}/capacities?api-version=7.1"
            resp2 = requests.get(url_cap, auth=("", pat), timeout=10)
            if resp2.status_code != 200:
                continue

            data = resp2.json()
            # API returns "teamMembers" (not "value")
            entries = data.get("teamMembers") or data.get("value", [])
            members = []
            for entry in entries:
                identity = entry.get("teamMember", {})
                name = identity.get("displayName", "")
                uid = identity.get("uniqueName", "")
                if name and not name.startswith("Azure"):
                    members.append({"name": name, "uniqueName": uid})
            if members:
                return sorted(members, key=lambda x: x["name"])
        return []
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=300)
def fetch_teams(pat, org, project):
    """Fetch all teams in the project, filtered to Core teams."""
    try:
        url = f"https://dev.azure.com/{org}/_apis/projects/{project}/teams?api-version=7.1"
        resp = requests.get(url, auth=("", pat), timeout=10)
        if resp.status_code != 200:
            return []
        teams = resp.json().get("value", [])
        # Filter to CoreProduct teams only (exclude DevsCore and others)
        core_teams = [t["name"] for t in teams if t.get("name", "").startswith("CoreProduct")]
        return sorted(core_teams) if core_teams else sorted([t["name"] for t in teams])
    except Exception:
        return []

@st.cache_data(show_spinner=False, ttl=300)
def fetch_team_members(pat, org, project, team="CoreProduct1"):
    """Fetch team members from Azure DevOps."""
    try:
        # Try exact name, then with/without " Team" suffix
        for team_name in [team, f"{team} Team", team.replace(" Team", "")]:
            url = f"https://dev.azure.com/{org}/_apis/projects/{project}/teams/{requests.utils.quote(team_name)}/members?api-version=7.1"
            resp = requests.get(url, auth=("", pat), timeout=10)
            if resp.status_code == 200:
                members = resp.json().get("value", [])
                result = []
                for m in members:
                    identity = m.get("identity", {})
                    name = identity.get("displayName", "")
                    uid = identity.get("uniqueName", "")
                    if name and not name.startswith("Azure"):
                        result.append({"name": name, "uniqueName": uid})
                return sorted(result, key=lambda x: x["name"])
        return []
    except Exception:
        return []


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
    project = get_project()

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
        if endalia_module:
            patch.append({"op": "replace", "path": "/fields/Custom.EndaliaModule", "value": endalia_module})
        if microservice:
            patch.append({"op": "replace", "path": "/fields/Custom.MicroserviceVersion", "value": microservice})
        if value_area:
            patch.append({"op": "replace", "path": "/fields/Microsoft.VSTS.Common.ValueArea", "value": value_area})
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
                "url": f"https://dev.azure.com/{get_org()}/_apis/wit/workItems/{parent_id}",
            }})
        patch_ops = [JsonPatchOperation(**p) for p in patch]
        return wit_client.create_work_item(document=patch_ops, project=project, type="Product Backlog Item")


def create_child_tasks(wit_client, project, pbi_id, task_titles, iteration_path=None, area_path=None, assignees=None):
    """Create Task work items as children of the given PBI, one per title in task_titles."""
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation
    org = get_org()
    created = []
    for i, title in enumerate(task_titles):
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
        if assignees and i < len(assignees) and assignees[i]:
            patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assignees[i]})
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
    if p.get("functional_spec"):
        h += f"<h3>📋 Especificación funcional</h3><p>{p['functional_spec'].replace(chr(10), '<br>')}</p>"
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


@st.cache_data(show_spinner=False)
def pbi_to_html_cached(pbi_json, figma_b64_tuple, figma_link):
    """Cached version - takes hashable args."""
    import json as _j
    p = _j.loads(pbi_json)
    figma_images_b64 = list(figma_b64_tuple) if figma_b64_tuple else None
    return _pbi_to_html_inner(p, figma_images_b64, figma_link)

def _pbi_to_html_inner(p, figma_images_b64=None, figma_link=None):
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
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )
    raw = "".join(block.text for block in response.content if block.type == "text")
    # Clean markdown fences and control characters
    clean = raw.replace("```json", "").replace("```", "").strip()
    # Remove control chars that break JSON parsing
    clean = re.sub(r'[--]', '', clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Try to extract JSON object if there's extra text around it
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


# ========== PBI CARD ==========

@st.fragment
def render_pbi_card(pbi, idx, total, default_iteration="", default_area="", default_module="", default_microservice="", default_value_area=""):
    import json as _json
    figma_b64 = []
    figma_link = st.session_state.get("figma_url", None)
    if "figma_images" in st.session_state:
        figma_b64 = [img.get("data", "") for img in st.session_state["figma_images"]]
    if "uploaded_b64" in st.session_state:
        figma_b64.extend(st.session_state["uploaded_b64"])

    import json as _j
    _cache_key = f"_html_{idx}"
    _pbi_hash = hash(_j.dumps(pbi, sort_keys=True, ensure_ascii=False))
    if st.session_state.get(f"_html_hash_{idx}") != _pbi_hash:
        try:
            html_content = pbi_to_html_cached(
                _j.dumps(pbi, ensure_ascii=False),
                tuple(figma_b64) if figma_b64 else (),
                figma_link or ""
            )
        except Exception:
            html_content = pbi_to_html(pbi, figma_b64, figma_link)
        st.session_state[_cache_key] = html_content
        st.session_state[f"_html_hash_{idx}"] = _pbi_hash
    else:
        html_content = st.session_state.get(_cache_key, "")
    pushed_key = f"pushed_{idx}"

    # ── Card header ──
    pushed_info = st.session_state.get(pushed_key, None)
    pushed_badge = f'<span class="pbi-pushed-badge">✅ #{pushed_info}</span>' if pushed_info else ""
    st.markdown(f"""
    <div class="pbi-card-header" style="border-radius:8px 8px 0 0;">
        <span class="pbi-card-title">{pbi['title']}</span>
        <span class="pbi-card-num">US {idx+1}/{total}</span>
        {pushed_badge}
    </div>
    """, unsafe_allow_html=True)

    # ── Action buttons ──
    col_copy, col_push = st.columns([1, 1])
    with col_copy:
        st.components.v1.html(f"""
        <div style="padding:4px 0;">
            <button onclick="copyHtml_{idx}()" style="width:100%;background:#6366f1;color:#fff;border:none;border-radius:8px;
                padding:9px 0;cursor:pointer;font-size:13px;font-weight:600;font-family:\'IBM Plex Sans\',sans-serif;">
                📋 Copiar para Azure
            </button>
            <div id="copied_{idx}" style="margin-top:6px;font-size:12px;color:#10b981;display:none;text-align:center;">✓ Copiado al portapapeles</div>
        </div>
        <script>
        async function copyHtml_{idx}() {{
            const html = {_json.dumps(html_content)};
            const plain = {_json.dumps(pbi['title'])};
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
            const s = document.getElementById("copied_{idx}");
            s.style.display = "block"; setTimeout(() => s.style.display = "none", 2500);
        }}
        </script>""", height=60)

    azure_available = bool(st.session_state.get("user_pat") or st.secrets.get("AZURE_PAT"))
    with col_push:
        if azure_available:
            with st.popover("🚀 Push to Azure DevOps", use_container_width=True):
                st.markdown(f"**`{pbi['title']}`**")
                mode = st.radio("Acción", ["Crear nuevo PBI", "Actualizar PBI existente"], key=f"mode_{idx}", horizontal=True)
                existing_id = None
                if mode == "Actualizar PBI existente":
                    existing_id_str = st.text_input("ID del Work Item", placeholder="Ej: 203734", key=f"existing_{idx}")
                    if existing_id_str:
                        existing_id = int(existing_id_str)

                c1, c2 = st.columns(2)
                with c1:
                    iteration = st.text_input("Iteration Path", value=default_iteration, key=f"iter_{idx}")
                    _modal_modules = st.session_state.get("_fetched_modules") or ["Registro y planificación horaria", "Vacaciones y ausencias"]
                    _emod_default = default_module if default_module in _modal_modules else _modal_modules[0]
                    endalia_module = st.selectbox("Endalia Module", _modal_modules,
                        index=_modal_modules.index(_emod_default),
                        key=f"emodule_{idx}")
                    value_area = st.selectbox("Value Area",
                        ["Product improvement", "Roadmap", "Operations improvement"],
                        key=f"varea_{idx}")
                with c2:
                    area = st.text_input("Area Path", value=default_area, key=f"area_{idx}")
                    microservice = st.selectbox("Microservice Version",
                        ["Candidate", "Candidate+1"],
                        index=0 if default_microservice not in ["Candidate","Candidate+1"]
                              else ["Candidate","Candidate+1"].index(default_microservice),
                        key=f"msvc_{idx}")

                parent = ""
                if mode == "Crear nuevo PBI":
                    parent = st.text_input("Parent Feature ID (opcional)", placeholder="Ej: 177040", key=f"parent_{idx}")
                    st.markdown("---")
                    create_tasks = st.checkbox("Crear task(s) hija(s) automáticamente", key=f"create_tasks_{idx}")
                    num_tasks = 1
                    task_titles = []
                    task_assignees = []
                    if create_tasks:
                        num_tasks = st.number_input("¿Cuántas tasks?", min_value=1, max_value=10, value=1, step=1, key=f"num_tasks_{idx}")
                        # Load team members
                        _pat = st.session_state.get("user_pat") or st.secrets.get("AZURE_PAT", "")
                        _org = st.session_state.get("user_org") or st.secrets.get("AZURE_ORG", "")
                        _proj = st.session_state.get("user_project") or st.secrets.get("AZURE_PROJECT", "")
                        _team = st.session_state.get("user_team") or st.session_state.get("user_team_select", "")
                        _iteration = st.session_state.get("default_iteration", "")
                        _members = []
                        if _team and _iteration and _iteration != "SWArea":
                            _members = fetch_sprint_members(_pat, _org, _proj, _team, _iteration)

                        if not _members and _team:
                            for _t in [_team, _team + " Team"]:
                                _members = fetch_team_members(_pat, _org, _proj, team=_t)
                                if _members:
                                    break
                        _member_names = ["— Sin asignar —"] + [m["name"] for m in _members]
                        _member_map = {"— Sin asignar —": ""} | {m["name"]: m["uniqueName"] for m in _members}

                        st.markdown("**Tasks**")
                        for t in range(int(num_tasks)):
                            tc1, tc2 = st.columns([3, 2])
                            with tc1:
                                title = st.text_input(f"Título task {t+1}", value=pbi["title"],
                                    key=f"task_title_{idx}_{t}", label_visibility="collapsed")
                                task_titles.append(title)
                            with tc2:
                                selected_name = st.selectbox(f"Asignar a {t+1}",
                                    _member_names, key=f"task_assignee_{idx}_{t}",
                                    label_visibility="collapsed")
                                task_assignees.append(_member_map.get(selected_name, ""))

                btn_label = "✅ Actualizar PBI" if existing_id else "✅ Crear PBI en Azure"
                if st.button(btn_label, key=f"push_{idx}", type="primary", use_container_width=True):
                    with st.spinner("Enviando a Azure DevOps..."):
                        try:
                            # Accept full Azure URL or bare ID
                            parent_id = None
                            if parent and parent.strip():
                                id_match = re.search(r'(\d+)/?$', parent.strip())
                                if id_match:
                                    parent_id = int(id_match.group(1))
                            result = push_pbi_to_azure(pbi,
                                iteration_path=iteration if iteration.strip() else None,
                                area_path=area if area.strip() else None,
                                parent_id=parent_id, figma_b64=figma_b64,
                                figma_link=figma_link, existing_id=existing_id,
                                endalia_module=endalia_module, microservice=microservice,
                                value_area=value_area)
                            pbi_url = f"https://dev.azure.com/{get_org()}/{get_project()}/_workitems/edit/{result.id}"
                            st.success(f"✅ PBI {'actualizado' if existing_id else 'creado'} — **#{result.id}** — [Abrir ↗]({pbi_url})")
                            st.session_state[pushed_key] = result.id

                            if mode == "Crear nuevo PBI" and create_tasks and num_tasks > 0:
                                with st.spinner(f"Creando {int(num_tasks)} task(s)..."):
                                    conn = get_azure_connection()
                                    wit_client = conn.clients.get_work_item_tracking_client()
                                    task_ids = create_child_tasks(wit_client,
                                        project=get_project(),
                                        pbi_id=result.id, task_titles=task_titles,
                                        iteration_path=iteration if iteration.strip() else None,
                                        area_path=area if area.strip() else None,
                                        assignees=task_assignees)
                                    st.success(f"✅ {len(task_ids)} task(s) — IDs: {', '.join(f'**{t}**' for t in task_ids)}")
                        except Exception as e:
                            st.error(f"Error: {e}")

    pbi["objective"] = st.text_input("🎯 Objetivo", pbi["objective"], key=f"obj_{idx}")

    if pbi.get("functional_spec"):
        st.markdown("**📋 Especificación funcional**")
        pbi["functional_spec"] = st.text_area(
            "spec", pbi["functional_spec"],
            key=f"spec_{idx}", height=200, label_visibility="collapsed"
        )

    st.markdown("**👤 Historia de Usuario**")
    pbi["role"] = st.text_input("Como", pbi["role"], key=f"role_{idx}", label_visibility="collapsed")
    pbi["when"] = st.text_input("Cuando", pbi["when"], key=f"when_{idx}", label_visibility="collapsed")
    pbi["then"] = st.text_input("Entonces", pbi["then"], key=f"then_{idx}", label_visibility="collapsed")
    pbi["benefit"] = st.text_input("Para", pbi["benefit"], key=f"ben_{idx}", label_visibility="collapsed")

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
        if figma_link:
            st.markdown(f"[🔗 Ver prototipo en Figma]({figma_link})")
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
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap');
html, body, [class*="css"] { font-family: 'IBM Plex Sans', sans-serif !important; }

.topbar {
    background:#ffffff;
    border-bottom:1px solid #e2e8f0;
    padding:0 28px;
    display:flex; align-items:center; justify-content:space-between;
    height:58px; margin:-1rem -1rem 1.5rem -1rem;
    box-shadow:0 1px 3px rgba(0,0,0,.06);
}
.topbar-brand { display:flex; align-items:center; gap:10px; }
.topbar-brand h1 {
    color:#0f172a !important; font-size:15px !important;
    font-weight:700 !important; margin:0 !important; letter-spacing:-.3px;
}
.topbar-divider { width:1px; height:20px; background:#e2e8f0; }
.topbar-sub { color:#94a3b8; font-size:12px; }
.topbar-badges { display:flex; gap:6px; align-items:center; }
.tbadge {
    display:inline-flex; align-items:center; gap:5px;
    background:#eff6ff; border:1px solid #bfdbfe;
    border-radius:20px; padding:4px 12px;
    font-size:12px; color:#3b82f6; font-weight:500; white-space:nowrap;
}
.tbadge .badge-label { color:#93c5fd; font-size:10px; text-transform:uppercase; letter-spacing:.5px; font-weight:600; }
.tbadge .badge-val { color:#1d4ed8; font-weight:700; }
.tbadge-zero { background:#f8fafc; border-color:#e2e8f0; }
.tbadge-zero .badge-val { color:#94a3b8; }

.stepper { display:flex; background:#f1f5f9; border-radius:10px; overflow:hidden;
    border:1px solid #e2e8f0; margin-bottom:20px; }
.step { flex:1; padding:9px 6px; text-align:center; font-size:12px; font-weight:500;
    color:#94a3b8; border-right:1px solid #e2e8f0; }
.step:last-child { border-right:none; }
.step.active { background:#2563EB; color:#fff; font-weight:700; }
.step.done { background:#f0fdf4; color:#16a34a; font-weight:600; }

.section-label { font-size:10px; font-weight:700; text-transform:uppercase;
    letter-spacing:1px; color:#64748b; margin-bottom:6px; }

.pbi-card-head { display:flex; align-items:center; justify-content:space-between;
    background:#0f172a; padding:10px 14px; border-radius:8px 8px 0 0; }
.pbi-card-title { color:#f8fafc; font-size:13px; font-weight:600; flex:1; margin-right:10px; line-height:1.3; }
.pbi-card-badge { background:#1e293b; color:#94a3b8; border-radius:4px;
    padding:2px 8px; font-size:11px; font-family:'IBM Plex Mono',monospace; flex-shrink:0; }
.pushed-badge { background:#064e3b; color:#6ee7b7; border-radius:4px;
    padding:2px 8px; font-size:11px; font-weight:700; margin-left:6px; flex-shrink:0; }

.pbi-us { border-left:3px solid #2563EB; background:#f8fafc; padding:10px 14px; border-radius:0 6px 6px 0; margin:6px 0; }
.pbi-ac { border-left:3px solid #10b981; background:#f0fdf4; padding:10px 14px; border-radius:0 6px 6px 0; margin:6px 0; }
.pbi-val { border-left:3px solid #f59e0b; background:#fffbeb; padding:10px 14px; border-radius:0 6px 6px 0; margin:6px 0; }
.pbi-err { border-left:3px solid #ef4444; background:#fef2f2; padding:10px 14px; border-radius:0 6px 6px 0; margin:6px 0; }
.block-label { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.8px; margin-bottom:6px; }

.stButton > button[kind="primary"] { background:#2563EB !important; border:none !important;
    border-radius:8px !important; font-weight:600 !important; font-size:14px !important; }
.stButton > button[kind="primary"]:hover { background:#1d4ed8 !important; }

.empty-panel { min-height:420px; display:flex; flex-direction:column;
    align-items:center; justify-content:center; border:2px dashed #e2e8f0;
    border-radius:16px; padding:48px; text-align:center; color:#94a3b8; }
</style>
""", unsafe_allow_html=True)

# ========== PAT LOGIN ==========

def render_login():
    st.markdown("""
    <style>
    .login-wrap { max-width:480px; margin:80px auto 0 auto; }
    .login-card { background:#fff; border:1px solid #e2e8f0; border-radius:16px;
        padding:40px; box-shadow:0 4px 24px rgba(0,0,0,.07); }
    .login-title { font-size:22px; font-weight:700; color:#0f172a; margin-bottom:6px; }
    .login-sub { font-size:14px; color:#64748b; margin-bottom:28px; line-height:1.5; }
    .login-hint { font-size:12px; color:#94a3b8; margin-top:16px; line-height:1.6; }
    .login-hint a { color:#2563EB; text-decoration:none; }
    </style>
    <div class="login-wrap">
      <div class="login-card">
        <div class="login-title">📋 Generador de PBIs</div>
        <div class="login-sub">Introduce tus credenciales de Azure DevOps para empezar.<br>
        Solo se guardan durante esta sesión.</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        lc1, lc2 = st.columns(2)
        with lc1:
            org = st.text_input("Organización Azure DevOps",
                placeholder="endalia",
                help="El nombre de tu organización en dev.azure.com/[org]")
        with lc2:
            project = st.text_input("Proyecto",
                placeholder="SWArea",
                help="El nombre del proyecto en Azure DevOps")
        pat = st.text_input("Personal Access Token (PAT)",
            type="password",
            help="Genera tu PAT en Azure DevOps → tu avatar → Personal access tokens → New Token → Work Items: Read & Write")
        submitted = st.form_submit_button("🔑 Conectar", type="primary", use_container_width=True)

        if submitted:
            if not org.strip() or not project.strip() or not pat.strip():
                st.error("Rellena todos los campos.")
            else:
                with st.spinner("Verificando credenciales..."):
                    try:
                        test_url = f"https://dev.azure.com/{org.strip()}/_apis/projects?api-version=7.1"
                        resp = requests.get(test_url, auth=("", pat.strip()), timeout=8)
                        if resp.status_code == 200:
                            st.session_state["user_pat"] = pat.strip()
                            st.session_state["user_org"] = org.strip()
                            st.session_state["user_project"] = project.strip()
                            # Auto-detect team
                            teams = fetch_teams(pat.strip(), org.strip(), project.strip())
                            if len(teams) == 1:
                                st.session_state["user_team"] = teams[0]
                            st.rerun()
                        elif resp.status_code == 401:
                            st.error("PAT incorrecto o sin permisos. Verifica que tenga acceso a Work Items.")
                        else:
                            st.error(f"No se pudo conectar ({resp.status_code}). Verifica la organización.")
                    except Exception as e:
                        st.error(f"Error de conexión: {e}")

    st.markdown("""
    <div style="max-width:480px;margin:12px auto 0 auto;font-size:12px;color:#94a3b8;line-height:1.7;">
    💡 <b>Cómo generar tu PAT:</b><br>
    Azure DevOps → tu avatar (arriba derecha) → <b>Personal access tokens</b> → New Token<br>
    Permisos necesarios: <b>Work Items → Read & Write</b>
    </div>
    """, unsafe_allow_html=True)

# ── Show login if no PAT ──
_logged_out = st.session_state.get("_logged_out", False)
_has_pat = (not _logged_out) and (st.session_state.get("user_pat") or st.secrets.get("AZURE_PAT"))
_has_org = (not _logged_out) and (st.session_state.get("user_org") or st.secrets.get("AZURE_ORG"))

if not _has_pat or not _has_org:
    render_login()
    st.stop()

# ── Top bar ──
n_pbis = len(st.session_state.get("result", {}).get("pbis", []))
module_badge = st.session_state.get("_last_module", "—")
sprint_badge = st.session_state.get("default_iteration", "—")
pushed_count = sum(1 for k in st.session_state if k.startswith("pushed_"))

_user_org = st.session_state.get("user_org") or st.secrets.get("AZURE_ORG", "")
_user_project = st.session_state.get("user_project") or st.secrets.get("AZURE_PROJECT", "")

st.markdown(f"""
<div class="topbar">
  <div class="topbar-brand">
    <span style="font-size:20px;">📋</span>
    <h1>Generador de PBIs</h1>
    <div class="topbar-divider"></div>
    <span class="topbar-sub">{_user_org} · {_user_project}</span>
  </div>
  <div class="topbar-badges">
    <div class="tbadge {'tbadge-zero' if n_pbis==0 else ''}">
      <span class="badge-label">PBIs</span>
      <span class="badge-val">{n_pbis}</span>
    </div>
    <div class="tbadge {'tbadge-zero' if pushed_count==0 else ''}">
      <span class="badge-label">Pusheados</span>
      <span class="badge-val">{pushed_count}</span>
    </div>
    <div class="tbadge tbadge-zero">
      <span class="badge-label">Módulo</span>
      <span class="badge-val">{module_badge}</span>
    </div>
    <div class="tbadge tbadge-zero">
      <span class="badge-label">Sprint</span>
      <span class="badge-val">{sprint_badge}</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# Logout button — separate, right-aligned, below topbar
logout_c1, logout_c2 = st.columns([11, 1])
with logout_c2:
    if st.button("🚪", help="Cerrar sesión", use_container_width=True):
        for k in list(st.session_state.keys()):
            if k not in ("default_iteration", "default_area", "default_module",
                         "default_microservice", "default_value_area"):
                st.session_state.pop(k, None)
        # Force show login even if secrets have AZURE_PAT
        st.session_state["_logged_out"] = True
        st.rerun()

# ── Layout ──
col_form, col_results = st.columns([5, 7], gap="large")

with col_form:
    # Stepper
    has_desc = bool(st.session_state.get("desc_input", ""))
    has_result = "result" in st.session_state
    step1 = "✓ Describe" if has_desc else "1 · Describe"
    step2 = "✓ Genera" if has_result else ("2 · Genera" if has_desc else "2 · Genera")
    step3 = "3 · Push Azure"
    s1 = "done" if has_desc else "active"
    s2 = "done" if has_result else ("active" if has_desc else "")
    s3 = "active" if has_result else ""
    st.markdown(f'''<div class="stepper">
      <div class="step {s1}">{step1}</div>
      <div class="step {s2}">{step2}</div>
      <div class="step {s3}">{step3}</div>
    </div>''', unsafe_allow_html=True)

    # ── Settings collapsible ──
    with st.expander("⚙️ Configuración (valores por defecto)", expanded=False):
        _pat = st.session_state.get("user_pat") or st.secrets.get("AZURE_PAT", "")
        _org = st.session_state.get("user_org") or st.secrets.get("AZURE_ORG", "")
        _proj = st.session_state.get("user_project") or st.secrets.get("AZURE_PROJECT", "")

        # Fetch area paths and modules dynamically
        _area_paths = fetch_area_paths(_pat, _org, _proj)
        _modules = fetch_modules(_pat, _org, _proj)
        if not _modules:
            _modules = ["Registro y planificación horaria", "Vacaciones y ausencias"]
        st.session_state["_fetched_modules"] = _modules

        # ── Area Path is the source of truth ──
        if _area_paths:
            _area_default = st.session_state.get("default_area", "")
            # Default to CoreProduct1 if nothing saved
            if not _area_default or _area_default not in _area_paths:
                _area_default = next((p for p in _area_paths if "CoreProduct1" in p), _area_paths[0])
            _area_idx = _area_paths.index(_area_default)
            default_area = st.selectbox("Mi área (Area Path)", _area_paths,
                index=_area_idx, key="default_area",
                help="Al cambiar el área se actualizan el equipo y el sprint automáticamente")
        else:
            default_area = st.text_input("Area Path", key="default_area",
                value=st.session_state.get("default_area", "SWArea\\Product\\Core\\CoreProduct1"))

        # ── Auto-derive team from area ──
        _derived_team = ""
        if default_area:
            import re as _re
            _m = _re.search("CoreProduct(\\d+)", default_area)
            if _m:
                _derived_team = f"CoreProduct{_m.group(1)}"
        if _derived_team:
            st.session_state["user_team"] = _derived_team
        _team_display = _derived_team or st.session_state.get("user_team", "—")

        # ── Auto-load sprint for derived team ──
        _iterations = fetch_iterations(_pat, _org, _proj, team=_team_display)
        _saved_iter = st.session_state.get("default_iteration", "")

        dcol1, dcol2 = st.columns(2)
        with dcol1:
            # Sprint selector — auto-loaded for derived team
            if _iterations:
                _iter_labels = [i["label"] for i in _iterations]
                _iter_paths = [i["path"] for i in _iterations]
                _iter_idx = _iter_paths.index(_saved_iter) if _saved_iter in _iter_paths else max(0, len(_iter_paths)-1)
                _selected_iter = st.selectbox("Sprint (Iteration)", _iter_labels,
                    index=_iter_idx, key="default_iteration_label")
                default_iteration = _iter_paths[_iter_labels.index(_selected_iter)]
                st.session_state["default_iteration"] = default_iteration
            else:
                default_iteration = st.text_input("Iteration Path", key="default_iteration",
                    value=_saved_iter or "SWArea")

            # Show derived team as read-only info
            st.caption(f"👥 Equipo derivado: **{_team_display}**")

        with dcol2:
            _module_idx = _modules.index(st.session_state["default_module"]) if st.session_state.get("default_module") in _modules else 0
            default_module = st.selectbox("Endalia Module", _modules,
                index=_module_idx, key="default_module")
            dcol_ms, dcol_va = st.columns(2)
            with dcol_ms:
                default_microservice = st.selectbox("Microservice",
                    ["Candidate", "Candidate+1"], key="default_microservice")
            with dcol_va:
                default_value_area = st.selectbox("Value Area",
                    ["Product improvement", "Roadmap", "Operations improvement"],
                    key="default_value_area")

        if st.button("🔄 Nuevo PBI — limpiar todo", use_container_width=True):
            for k in ["result", "figma_images", "uploaded_b64", "last_voice_text",
                      "figma_url", "_last_module", "desc_input"]:
                st.session_state.pop(k, None)
            for k in list(st.session_state.keys()):
                if k.startswith("pushed_"):
                    del st.session_state[k]
            st.rerun()

    # ── Main form ──
    with st.container(border=True):
        c1, c2 = st.columns(2)
        with c1:
            module = st.text_input("Módulo", placeholder="Ej: Holidays & Absences", key="module_input")
        with c2:
            feature = st.text_input("Feature", placeholder="Ej: Políticas de V&A", key="feature_input")

        # Description with complexity indicator
        description = st.text_area(
            "Descripción funcional *",
            placeholder="Desde algo breve ('quitar validación de suma, cada campo 0-100') hasta una feature completa...",
            height=160, key="desc_input"
        )
        desc_len = len(description)
        if desc_len == 0:
            st.caption("")
        elif desc_len < 80:
            st.caption(f"🟢 Cambio puntual — 1 PBI esperado · {desc_len} caracteres")
        elif desc_len < 300:
            st.caption(f"🟡 Feature media — 1-2 PBIs · {desc_len} caracteres")
        else:
            st.caption(f"🔴 Feature compleja — 2+ PBIs · {desc_len} caracteres")

        with st.container():
            st.markdown('<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#64748b;margin-bottom:4px;">🎤 Dictar con voz</div>', unsafe_allow_html=True)
            voice_text = speech_to_text(start_prompt="⏺️ Iniciar grabación",
                stop_prompt="⏹️ Parar grabación", language="es",
                use_container_width=True, key="voice_recorder")
            if voice_text:
                st.session_state["last_voice_text"] = voice_text
            if st.session_state.get("last_voice_text"):
                st.caption("Texto dictado — cópialo y pégalo en la descripción:")
                st.code(st.session_state["last_voice_text"], language=None)

        context = st.text_area("Contexto técnico (opcional)",
            placeholder="Endpoints, dependencias, restricciones...", height=70, key="context_input")

        st.markdown("**🎨 Prototipo**")
        figma_available = "FIGMA_TOKEN" in st.secrets
        tab_figma, tab_upload = st.tabs(["🔗 Figma", "📁 Capturas"])

        with tab_figma:
            if figma_available:
                figma_url = st.text_input("URL del prototipo",
                    placeholder="https://www.figma.com/proto/...", key="figma_url")
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
            uploaded_files = st.file_uploader("Sube capturas",
                type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
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
    module = st.session_state.get("module_input", "")
    feature = st.session_state.get("feature_input", "")
    context = st.session_state.get("context_input", "")
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
                    st.rerun()
                except Exception as e:
                    st.error(f"Error al generar: {e}")


# ========== DISPLAY RESULTS ==========

with col_results:
    if "result" in st.session_state:
        result = st.session_state["result"]
        n = len(result["pbis"])

        rc1, rc2 = st.columns([6, 2])
        with rc1:
            st.markdown(f"<div style='font-size:17px;font-weight:700;color:#0f172a;'>PBIs generados</div>", unsafe_allow_html=True)
        with rc2:
            st.markdown(f"<div style='background:#2563EB;color:white;border-radius:20px;padding:4px 14px;font-size:13px;font-weight:600;text-align:center;'>{n} PBI{'s' if n!=1 else ''}</div>", unsafe_allow_html=True)

        if result.get("summary"):
            st.info(f"💡 {result['summary']}")

        for i, pbi in enumerate(result["pbis"]):
            with st.expander(f"{'✅ ' if st.session_state.get(f'pushed_{i}') else ''}US {i+1}/{n} — {pbi['title']}", expanded=True):
                render_pbi_card(pbi, i, n,
                    default_iteration=st.session_state.get("default_iteration", ""),
                    default_area=st.session_state.get("default_area", ""),
                    default_module=st.session_state.get("default_module", "Registro y planificación horaria"),
                    default_microservice=st.session_state.get("default_microservice", "Candidate"),
                    default_value_area=st.session_state.get("default_value_area", "Product improvement"))
    else:
        st.markdown("""
        <div class="empty-panel">
            <div style="font-size:48px;margin-bottom:16px;">📋</div>
            <div style="font-size:16px;font-weight:700;color:#475569;margin-bottom:8px;">Aquí aparecerán tus PBIs</div>
            <div style="font-size:13px;max-width:240px;line-height:1.6;">
                Describe la funcionalidad en el formulario y pulsa <b>Generar PBIs</b>
            </div>
            <div style="margin-top:24px;display:flex;gap:16px;font-size:12px;color:#94a3b8;">
                <span>✍️ Texto libre</span>
                <span>🖼️ Capturas Figma</span>
                <span>🎤 Dictado</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
