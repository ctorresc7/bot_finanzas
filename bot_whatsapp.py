"""
BOT WHATSAPP — FINANZAS HOGAR
Sthefanie & Christian

Stack: WhatsApp Cloud API (Meta) + Flask + Claude API + Google Sheets
Hosting: Railway

Flujo:
  1. WhatsApp message → Meta webhook → este script
  2. Claude clasifica el gasto/ingreso
  3. Bot responde pidiendo confirmación
  4. Usuario responde "si" → se escribe en Google Sheets
"""

import os, json, re, requests
from datetime import datetime, timezone, timedelta
LIMA_TZ = timezone(timedelta(hours=-5))
from flask import Flask, request, jsonify
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ['ANTHROPIC_API_KEY']
WA_TOKEN           = os.environ['WA_TOKEN']          # Token de acceso de Meta
WA_PHONE_ID        = os.environ['WA_PHONE_ID']       # ID del número de WhatsApp
WA_VERIFY_TOKEN    = os.environ['WA_VERIFY_TOKEN']   # Token que tú inventas para verificar webhook
GOOGLE_SHEET_ID    = os.environ['GOOGLE_SHEET_ID']
GOOGLE_CREDS_JSON  = os.environ['GOOGLE_CREDS_JSON']

USUARIOS = {
    os.environ.get('PHONE_STHEFANIE', '51000000000'): 'Sthefanie',
    os.environ.get('PHONE_CHRISTIAN',  '51000000001'): 'Christian',
}

# ── CLIENTES ──────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_sheet():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID).worksheet('REGISTRO')

# ── ESTADO DE CONVERSACIÓN ────────────────────────────────────────────────────
sessions = {}

# ── CLASIFICADOR CON CLAUDE ───────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres el asistente financiero del hogar de Sthefanie y Christian en Lima, Perú.
Tu única función es interpretar mensajes de gastos/ingresos en lenguaje natural y devolver un JSON estructurado.

Reglas:
- Responde SOLO con JSON, sin texto adicional, sin markdown.
- Si no encuentras el monto, pon null en "importe".
- Detecta el medio de pago si lo mencionan.
- Detecta si es en cuotas (ej: "3 cuotas", "cuotas").
- Clasifica el tipo: Ingreso, Gasto o Ahorro.
- Elige la categoría más apropiada de esta lista exacta:
  Ingresos: Sueldo Sthefanie, Sueldo Christian, Alquiler auto, Tarjeta alimentos, Intereses / inversiones, Otros ingresos
  Gastos fijos: Scotiabank Platinum, Scotiabank Singular, Falabella CMR, Terreno, Apoyo sobrino, Telefonía, Servicios Sthefanie, Servicios Christian
  Gastos variables: Supermercado / mercado, Transporte / taxi / Uber, Gasolina, Salud / farmacia, Médico / clínica, Educación, Higiene personal, Ropa / calzado
  No esencial: Restaurantes, Delivery, Salidas / ocio, Cine / entretenimiento, Regalos, Viajes, Suscripciones, Gastos hormiga, Otros no esenciales
  Ahorros: Fondo de emergencia, Meta ahorro (viajes), Inversiones, Otros ahorros
- Si NINGUNA categoría de la lista encaja bien, pon "categoria_nueva": true y sugiere un nombre en "categoria_sugerida".
- Incluye siempre un campo "confianza": "alta", "media" o "baja" según qué tan seguro estás de la categoría.
- "baja" = no estás seguro, "media" = probable, "alta" = muy seguro.

Formato JSON de respuesta:
{
  "descripcion": "texto limpio del gasto",
  "tipo": "Gasto|Ingreso|Ahorro",
  "grupo": "nombre del grupo",
  "categoria": "nombre exacto de la categoría",
  "importe": 45.00,
  "medio": "Efectivo|Yape/Plin|Scotiabank Platinum|Scotiabank Singular|Falabella CMR|Transferencia|Débito",
  "cuotas": "-",
  "confianza": "alta|media|baja",
  "categoria_nueva": false,
  "categoria_sugerida": null
}"""

def clasificar(texto):
    try:
        resp = claude.messages.create(
            model=os.environ.get('ANTHROPIC_MODEL', 'claude-3-haiku-20240307'),
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': texto}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r'^```json|```$', '', raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        return {'error': str(e)}

# ── ENVIAR MENSAJE POR META ───────────────────────────────────────────────────
def send_message(to, text):
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=payload)

# ── MENSAJE DE CONFIRMACIÓN ───────────────────────────────────────────────────
EMOJI = {
    'Ingreso': '🟢', 'Gasto': '🔴', 'Ahorro': '🔵',
    'No esencial': '🟣', 'Variable': '🟡', 'Fijo hogar': '🔴',
    'Tarjeta': '💳', 'Sueldo': '💼', 'Alquiler auto': '🚗',
}

def build_confirm_msg(data, quien):
    e = EMOJI.get(data.get('grupo',''), EMOJI.get(data.get('tipo',''), '📝'))
    importe = f"S/ {data['importe']:,.2f}" if data.get('importe') else '❓ no detecté el monto'
    cuotas = f" · {data['cuotas']} cuotas" if data.get('cuotas') and data['cuotas'] != '-' else ''
    lines = [
        "📋 *Antes de guardar, confirma:*",
        "",
        f"{e} *{data.get('descripcion','—')}*",
        f"📂 {data.get('tipo','—')} → {data.get('grupo','—')} → {data.get('categoria','—')}",
        f"💰 {importe}{cuotas}",
        f"💳 {data.get('medio','—')}",
        f"👤 {quien}  ·  📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
    ]
    if data.get('duda'):
        lines.append(f"🤔 *Tengo una duda:* {data.get('pregunta','')}")
        lines.append("")
    lines.append("Responde *sí* para guardar, o corrígeme lo que esté mal.")
    return '\n'.join(lines)

# ── GUARDAR EN GOOGLE SHEETS ──────────────────────────────────────────────────

def agregar_categoria_nueva(tipo, grupo, categoria, palabras_clave=''):
    """Agrega una nueva categoría a la hoja CATEGORIAS del sheet."""
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        from google.oauth2.service_account import Credentials as Creds3
        creds3 = Creds3.from_service_account_info(creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        import gspread as gs3
        gc3 = gs3.authorize(creds3)
        sheet_cats = gc3.open_by_key(GOOGLE_SHEET_ID).worksheet('CATEGORIAS')
        sheet_cats.append_row([tipo, grupo, categoria, palabras_clave], value_input_option='USER_ENTERED')
        return True
    except Exception as ex:
        return False

def guardar_en_sheet(data, quien):
    now = datetime.now(LIMA_TZ)
    sheet = get_sheet()
    row = [
        now.strftime('%d/%m/%Y'),
        now.strftime('%H:%M'),
        data.get('descripcion', ''),
        data.get('tipo', ''),
        data.get('grupo', ''),
        data.get('categoria', ''),
        data.get('importe', 0),
        data.get('medio', ''),
        data.get('cuotas', '-'),
        quien,
        now.month,
        data.get('notas', ''),
    ]
    sheet.append_row(row, value_input_option='USER_ENTERED')

    # Formato automático por tipo
    try:
        tipo = data.get('tipo', 'Gasto')
        color_map = {'Ingreso': 'E6F4EC', 'Ahorro': 'E1F5EE', 'Gasto': 'FCEBEB'}
        bg = color_map.get(tipo, 'FFFFFF')
        last_row = len(sheet.get_all_values())
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        from google.oauth2.service_account import Credentials as Creds2
        creds2 = Creds2.from_service_account_info(creds_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        import gspread as gs2
        gc2 = gs2.authorize(creds2)
        spreadsheet = gc2.open_by_key(GOOGLE_SHEET_ID)
        sheet_id = spreadsheet.worksheet('REGISTRO').id
        def hex_rgb(h):
            return {'red':int(h[0:2],16)/255,'green':int(h[2:4],16)/255,'blue':int(h[4:6],16)/255}
        thin = {'style':'SOLID','color':{'red':0.8,'green':0.8,'blue':0.8},'width':1}
        spreadsheet.batch_update({'requests':[
            {'repeatCell':{
                'range':{'sheetId':sheet_id,'startRowIndex':last_row-1,'endRowIndex':last_row,'startColumnIndex':0,'endColumnIndex':12},
                'cell':{'userEnteredFormat':{'backgroundColor':hex_rgb(bg),'textFormat':{'fontFamily':'Arial','fontSize':9},'verticalAlignment':'MIDDLE','borders':{'top':thin,'bottom':thin,'left':thin,'right':thin}}},
                'fields':'userEnteredFormat(backgroundColor,textFormat,verticalAlignment,borders)'
            }},
            {'repeatCell':{
                'range':{'sheetId':sheet_id,'startRowIndex':last_row-1,'endRowIndex':last_row,'startColumnIndex':6,'endColumnIndex':7},
                'cell':{'userEnteredFormat':{'numberFormat':{'type':'CURRENCY','pattern':'S/ #,##0.00'}}},
                'fields':'userEnteredFormat.numberFormat'
            }},
        ]})
    except Exception:
        pass

# ── CORRECCIONES ──────────────────────────────────────────────────────────────
def aplicar_correccion(pending, texto):
    t = texto.lower()
    m = re.search(r'(\d+[\.,]?\d*)', t)
    if m and any(w in t for w in ['monto','importe','son','fue','es']):
        pending['importe'] = float(m.group(1).replace(',','.'))
    for medio in ['efectivo','yape','plin','transferencia','débito','platinum','singular','cmr','falabella']:
        if medio in t:
            mapping = {
                'platinum':'Scotiabank Platinum','singular':'Scotiabank Singular',
                'falabella':'Falabella CMR','cmr':'Falabella CMR',
                'yape':'Yape/Plin','plin':'Yape/Plin',
                'efectivo':'Efectivo','transferencia':'Transferencia','débito':'Débito',
            }
            pending['medio'] = mapping.get(medio, medio.capitalize())
    m2 = re.search(r'(\d+)\s*cuotas?', t)
    if m2:
        pending['cuotas'] = m2.group(1)
    return pending

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/webhook', methods=['GET'])
def verify():
    """Meta llama a este endpoint para verificar el webhook"""
    mode      = request.args.get('hub.mode')
    token     = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        entry    = data['entry'][0]
        changes  = entry['changes'][0]
        value    = changes['value']

        # Ignorar notificaciones de estado (delivered, read, etc.)
        if 'messages' not in value:
            return jsonify({'status': 'ok'})

        msg      = value['messages'][0]
        from_num = msg['from']          # ej: 51997932962
        body     = msg['text']['body'].strip()
        quien    = USUARIOS.get(from_num, 'Desconocido')

    except (KeyError, IndexError):
        return jsonify({'status': 'ok'})

    if quien == 'Desconocido':
        send_message(from_num, '⚠️ Número no autorizado.')
        return jsonify({'status': 'ok'})

    sess = sessions.get(from_num, {})

    # ── Comandos especiales ────────────────────────────────────────────────────
    if body.lower() in ['ayuda', 'help', '?']:
        send_message(from_num,
            "📖 *Cómo registrar:*\n\n"
            "Escribe en lenguaje natural:\n"
            "• almuerzo pollería 45 efectivo\n"
            "• Rappi 35 yape\n"
            "• Scotiabank Platinum 4005 transferencia\n"
            "• sueldo mayo ingreso\n"
            "• yape auto 350 ingreso\n"
            "• ropa Saga 230 Falabella 3 cuotas\n\n"
            "Escribe *cancelar* para anular."
        )
        return jsonify({'status': 'ok'})

    if body.lower() in ['cancelar', 'cancel', 'no']:
        sessions.pop(from_num, None)
        send_message(from_num, '❌ Registro cancelado.')
        return jsonify({'status': 'ok'})

    # ── Confirmación pendiente ─────────────────────────────────────────────────
    if sess.get('step') == 'confirm':
        t = body.lower().strip()

        # Guardar con categoría nueva confirmada
        if t == 'nueva' or sess.get('step') == 'nueva_cat':
            pending = sess.get('pending', {})
            cat_sugerida = pending.get('categoria_sugerida') or pending.get('categoria', 'Nueva categoría')
            tipo_cat = pending.get('tipo', 'Gasto')
            grupo_cat = pending.get('grupo', 'Variable')

            if t == 'nueva':
                # Pedir confirmación del nombre
                sessions[from_num] = {'step': 'nueva_cat', 'pending': pending}
                send_message(from_num,
                    f"🆕 *Crear nueva categoría*\n\n"
                    f"Nombre sugerido: *{cat_sugerida}*\n"
                    f"Tipo: {tipo_cat} | Grupo: {grupo_cat}\n\n"
                    f"Responde *sí* para crearla con ese nombre, o escribe el nombre que prefieras."
                )
                return jsonify({'status': 'ok'})

            if sess.get('step') == 'nueva_cat':
                # Confirmar o usar nombre escrito
                nombre_final = cat_sugerida if t in ['si','sí','yes','ok'] else body.strip()
                pending['categoria'] = nombre_final
                ok = agregar_categoria_nueva(tipo_cat, grupo_cat, nombre_final)
                guardar_en_sheet(pending, quien)
                imp = pending.get('importe', 0)
                msg_cat = (f"✅ *¡Guardado!*\n"
                           f"🆕 Categoría *{nombre_final}* {'agregada al sheet' if ok else '(no se pudo agregar al sheet)'}\n"
                           f"📊 {pending.get('descripcion','')} — S/ {imp:,.2f}")
                send_message(from_num, msg_cat)
                sessions.pop(from_num, None)
                return jsonify({'status': 'ok'})

        # Confirmación positiva normal
        if t in ['si','sí','yes','ok','dale','confirmar','guardar','correcto','bueno','ya']:
            try:
                guardar_en_sheet(sess['pending'], quien)
                imp  = sess['pending'].get('importe', 0)
                tipo = sess['pending'].get('tipo', 'Gasto')
                e    = '🟢' if tipo == 'Ingreso' else ('🔵' if tipo == 'Ahorro' else '🔴')
                cat  = sess['pending'].get('categoria','')
                send_message(from_num,
                    f"✅ *¡Guardado!*\n"
                    f"{e} {sess['pending'].get('descripcion','')} — S/ {imp:,.2f}\n"
                    f"🏷️ {cat}\n"
                    f"_Anotado en Google Sheets_ 📊"
                )
                sessions.pop(from_num, None)
            except Exception as ex:
                send_message(from_num, f"⚠️ Error al guardar: {ex}")

        # Corrección de categoría por confianza baja
        else:
            pending = sess['pending']
            # Si escribe una categoría nueva directamente
            if len(body) > 3 and body[0].isupper():
                pending['categoria'] = body.strip()
                pending['confianza'] = 'alta'
            else:
                pending = aplicar_correccion(pending, body)
            sessions[from_num] = {'step': 'confirm', 'pending': pending}
            msg_text = build_confirm_msg(pending, quien)
            msg_text += '\n\n_Apliqué tu corrección. ¿Ahora sí?_'
            send_message(from_num, msg_text)
        return jsonify({'status': 'ok'})

    # ── Nuevo mensaje ──────────────────────────────────────────────────────────
    data_cl = clasificar(body)

    if 'error' in data_cl:
        send_message(from_num, f"⚠️ Error técnico: {data_cl.get('error','desconocido')[:200]}")
        return jsonify({'status': 'ok'})

    if not data_cl.get('importe'):
        sessions[from_num] = {'step': 'confirm', 'pending': data_cl}
        send_message(from_num,
            f"Entendí: *{data_cl.get('descripcion','—')}*\n"
            f"Categoría: {data_cl.get('categoria','—')}\n\n"
            f"🤔 ¿Cuánto fue el monto?"
        )
        return jsonify({'status': 'ok'})

    sessions[from_num] = {'step': 'confirm', 'pending': data_cl}
    send_message(from_num, build_confirm_msg(data_cl, quien))
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
