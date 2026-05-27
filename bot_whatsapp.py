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
from datetime import datetime
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
  No esenciales: Restaurantes, Delivery, Salidas / ocio, Cine / entretenimiento, Regalos, Viajes, Suscripciones, Gastos hormiga, Otros no esenciales
  Ahorros: Fondo de emergencia, Meta ahorro (viajes), Inversiones, Otros ahorros
- Si tienes dudas sobre la categoría, pon "duda": true y explica en "pregunta" qué necesitas saber.

Formato JSON de respuesta:
{
  "descripcion": "texto limpio del gasto",
  "tipo": "Gasto|Ingreso|Ahorro",
  "grupo": "nombre del grupo",
  "categoria": "nombre exacto de la categoría",
  "importe": 45.00,
  "medio": "Efectivo|Yape/Plin|Scotiabank Platinum|Scotiabank Singular|Falabella CMR|Transferencia|Débito",
  "cuotas": "-",
  "duda": false,
  "pregunta": null
}"""

def clasificar(texto):
    try:
        resp = claude.messages.create(
            model='claude-sonnet-4-20250514',
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
def guardar_en_sheet(data, quien):
    now = datetime.now()
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
        if t in ['si','sí','yes','ok','dale','confirmar','guardar','correcto','bueno','ya']:
            try:
                guardar_en_sheet(sess['pending'], quien)
                imp  = sess['pending'].get('importe', 0)
                tipo = sess['pending'].get('tipo', 'Gasto')
                e    = '🟢' if tipo == 'Ingreso' else ('🔵' if tipo == 'Ahorro' else '🔴')
                send_message(from_num,
                    f"✅ *¡Guardado!*\n"
                    f"{e} {sess['pending'].get('descripcion','')} — S/ {imp:,.2f}\n"
                    f"_Anotado en Google Sheets_ 📊"
                )
                sessions.pop(from_num, None)
            except Exception as ex:
                send_message(from_num, f"⚠️ Error al guardar: {ex}")
        else:
            sess['pending'] = aplicar_correccion(sess['pending'], body)
            sessions[from_num] = sess
            msg_text = build_confirm_msg(sess['pending'], quien)
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
