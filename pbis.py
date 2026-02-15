import streamlit as st
import anthropic
import json
import base64
from io import BytesIO

st.set_page_config(page_title="Generador de PBIs", page_icon="📋", layout="wide")

SYSTEM_PROMPT = """Eres un asistente experto en Product Management que genera Product Backlog Items (PBIs) para Azure DevOps.

EL INPUT DEL USUARIO PUEDE SER:
- Texto breve e informal, incluso dictado por voz. Tu trabajo es estructurarlo.
- Una descripción larga de una feature completa. Tu trabajo es proponer la división óptima.
- Con 2-3 frases + capturas puedes generar un PBI completo.

REGLAS DE DIVISIÓN:
- Evalúa la complejidad REAL. Un cambio de validación puntual = 1 PBI.
- Solo divide cuando hay flujos independientes con complejidad suficiente.
- En "summary", JUSTIFICA tu decisión: "Es 1 solo PBI porque..." o "Se divide en X PBIs porque..."
- Si divides, explica qué criterio usaste.

FORMATO DE CADA PBI:
- Título: [Módulo] - [Feature] - US X.X - [Acción concreta y alcance]
- Objetivo: UNA frase concisa
- Historia de Usuario:
  * COMO [rol]
  * CUANDO [ruta navegación / pantalla / contexto]
  * ENTONCES [acción y resultado específico]
  * PARA [beneficio]
- Criterios de Aceptación:
  * Happy Path: flujo principal, concisos
  * Validaciones: solo las relevantes
  * Errores: solo si aplica
- Prototipo: refs a capturas si las hay
- Dependencias: solo si hay múltiples PBIs relacionados
- Notas Técnicas: preguntas relevantes para dev

CONCISIÓN: ACs directos, 1 línea por AC. No repitas info de la historia. No infles.

RESPONDE SOLO JSON válido sin backticks:
{
  "summary": "Justificación de la división",
  "pbis": [{
    "title": "...", "objective": "...", "role": "...", "when": "...", "then": "...", "benefit": "...",
    "happy_path": ["AC1: ..."], "validations": ["AC-V1: ..."], "error_states": ["AC-E1: ..."],
    "prototype_refs": ["(Captura X) ..."], "dependencies": [], "tech_notes": ["..."]
  }]
}"""


def pbi_to_html(p):
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
    if p.get("prototype_refs"):
        h += "<h3>🖼️ Prototipo</h3><ul>"
        for r in p["prototype_refs"]:
            h += f"<li>{r}</li>"
        h += "</ul>"
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


def generate_pbis(module, feature, description, context, images):
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])

    user_content = []
    text = f"MÓDULO: {module or 'No especificado'}\nFEATURE: {feature or 'No especificada'}\n\nDESCRIPCIÓN:\n{description}"
    if context:
        text += f"\n\nCONTEXTO TÉCNICO:\n{context}"
    if images:
        text += f"\n\nSe adjuntan {len(images)} captura(s) del prototipo (Captura 1, 2...). Analízalas y referéncialas en los PBIs."

    user_content.append({"type": "text", "text": text})

    for img_data, media_type in images:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": img_data}
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


def render_pbi_card(pbi, idx, total):
    with st.container():
        st.markdown(f"### US {idx+1}/{total} — {pbi['title']}")

        # Copy button
        html_content = pbi_to_html(pbi)
        st.components.v1.html(f"""
        <div>
            <button onclick="copyHtml()" id="copyBtn_{idx}" style="background:#6366f1;color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:13px;font-weight:600;">
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

        # Editable fields
        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown("**🎯 Objetivo**")
        with col2:
            pbi["objective"] = st.text_input("obj", pbi["objective"], key=f"obj_{idx}", label_visibility="collapsed")

        st.markdown("**👤 Historia de Usuario**")
        c1, c2 = st.columns([0.15, 0.85])
        with c1:
            st.markdown("**Como**")
        with c2:
            pbi["role"] = st.text_input("r", pbi["role"], key=f"role_{idx}", label_visibility="collapsed")

        c1, c2 = st.columns([0.15, 0.85])
        with c1:
            st.markdown("**Cuando**")
        with c2:
            pbi["when"] = st.text_input("w", pbi["when"], key=f"when_{idx}", label_visibility="collapsed")

        c1, c2 = st.columns([0.15, 0.85])
        with c1:
            st.markdown("**Entonces**")
        with c2:
            pbi["then"] = st.text_input("t", pbi["then"], key=f"then_{idx}", label_visibility="collapsed")

        c1, c2 = st.columns([0.15, 0.85])
        with c1:
            st.markdown("**Para**")
        with c2:
            pbi["benefit"] = st.text_input("b", pbi["benefit"], key=f"ben_{idx}", label_visibility="collapsed")

        st.markdown("**✅ Happy Path**")
        for i, ac in enumerate(pbi.get("happy_path", [])):
            pbi["happy_path"][i] = st.text_input(f"hp{i}", ac, key=f"hp_{idx}_{i}", label_visibility="collapsed")

        if pbi.get("validations"):
            st.markdown("**⚠️ Validaciones y Edge Cases**")
            for i, v in enumerate(pbi["validations"]):
                pbi["validations"][i] = st.text_input(f"v{i}", v, key=f"v_{idx}_{i}", label_visibility="collapsed")

        if pbi.get("error_states"):
            st.markdown("**🚨 Estados de Error**")
            for i, e in enumerate(pbi["error_states"]):
                pbi["error_states"][i] = st.text_input(f"e{i}", e, key=f"e_{idx}_{i}", label_visibility="collapsed")

        if pbi.get("prototype_refs"):
            st.markdown("**🖼️ Prototipo**")
            for i, r in enumerate(pbi["prototype_refs"]):
                pbi["prototype_refs"][i] = st.text_input(f"pr{i}", r, key=f"pr_{idx}_{i}", label_visibility="collapsed")

        if pbi.get("tech_notes"):
            st.markdown("**💡 Notas Técnicas**")
            for i, n in enumerate(pbi["tech_notes"]):
                pbi["tech_notes"][i] = st.text_input(f"tn{i}", n, key=f"tn_{idx}_{i}", label_visibility="collapsed")

        st.divider()


# ========== MAIN UI ==========

st.title("📋 Generador de PBIs")
st.caption("Describe la funcionalidad → genera, edita y copia PBIs para Azure DevOps")

# Input form
with st.container(border=True):
    col1, col2 = st.columns(2)
    with col1:
        module = st.text_input("Módulo", placeholder="Ej: Holidays & Absences")
    with col2:
        feature = st.text_input("Feature", placeholder="Ej: Políticas de V&A")

    description = st.text_area(
        "Descripción funcional *",
        placeholder="Desde algo breve ('quitar validación de suma, cada campo 0-100') hasta una feature completa...",
        height=150
    )

    context = st.text_area(
        "Contexto técnico (opcional)",
        placeholder="Endpoints, dependencias, restricciones...",
        height=80
    )

    uploaded_files = st.file_uploader(
        "Capturas del prototipo",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        help="Sube capturas de Figma o cualquier imagen del prototipo"
    )

    # Show uploaded images
    if uploaded_files:
        cols = st.columns(min(len(uploaded_files), 5))
        for i, f in enumerate(uploaded_files):
            with cols[i % 5]:
                st.image(f, caption=f"Captura {i+1}", width=120)

    generate_btn = st.button("🚀 Generar PBIs", type="primary", use_container_width=True)


# Process
if generate_btn:
    if not description.strip():
        st.error("Añade una descripción funcional")
    else:
        images = []
        if uploaded_files:
            for f in uploaded_files:
                b64 = base64.b64encode(f.read()).decode("utf-8")
                mt = f.type or "image/png"
                images.append((b64, mt))

        with st.spinner("Analizando y generando PBIs..."):
            try:
                result = generate_pbis(module, feature, description, context, images)
                st.session_state["result"] = result
            except Exception as e:
                st.error(f"Error al generar: {e}")


# Display results
if "result" in st.session_state:
    result = st.session_state["result"]

    st.markdown(f"## PBIs Generados ({len(result['pbis'])})")

    if result.get("summary"):
        st.info(f"💡 **Análisis de división:** {result['summary']}")

    for i, pbi in enumerate(result["pbis"]):
        with st.expander(f"US {i+1}/{len(result['pbis'])} — {pbi['title']}", expanded=True):
            render_pbi_card(pbi, i, len(result["pbis"]))
