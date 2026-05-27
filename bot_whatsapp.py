"""
BOT WHATSAPP — FINANZAS HOGAR
Sthefanie & Christian

Stack: Twilio (WhatsApp) + Flask + Claude API + Google Sheets API
Hosting: Railway / Render (gratis)

Flujo:
  1. WhatsApp message → Twilio webhook → este script
  2. Claude clasifica el gasto/ingreso
  3. Bot responde pidiendo confirmación
  4. Usuario responde "si" → se escribe en Google Sheets
"""

import os, json, re
from datetime import datetime
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ['ANTHROPIC_API_KEY']
TWILIO_ACCOUNT_SID  = os.environ['TWILIO_ACCOUNT_SID']
TWILIO_AUTH_TOKEN   = os.environ['TWILIO_AUTH_TOKEN']
TWILIO_WHATSAPP_NUM = os.environ['TWILIO_WHATSAPP_NUM']   # whatsapp:+14155238886
GOOGLE_SHEET_ID     = os.environ['GOOGLE_SHEET_ID']       # ID del Google Sheet
GOOGLE_CREDS_JSON   = os.environ['GOOGLE_CREDS_JSON']     # JSON de service account

# Números autorizados → nombre
USUARIOS = {
    os.environ.get('PHONE_STHEFANIE', '+51000000000'): 'Sthefanie',
    os.environ.get('PHONE_CHRISTIAN',  '+51000000001'): 'Christian',
}

# ── CLIENTES ──────────────────────────────────────────────────────────────────
claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio  = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def get_sheet():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID).worksheet('REGISTRO')

# ── ESTADO DE CONVERSACIÓN (en memoria, simple) ───────────────────────────────
# { phone: { 'pending': {...}, 'step': 'confirm' } }
sessions = {}

# ── CATEGORIAS (espejo de la hoja CATEGORIAS) ─────────────────────────────────
CATEGORIAS = [
    ('Ingreso','Sueldo','Sueldo Sthefanie',['sueldo sthefanie','pago sthefanie']),
    ('Ingreso','Sueldo','Sueldo Christian',['sueldo christian','pago christian']),
    ('Ingreso','Alquiler auto','Alquiler auto',['yape auto','alquiler auto','renta auto',' auto ']),
    ('Ingreso','Tarjeta alimentos','Tarjeta alimentos',['tarjeta alimentos','vale alimentos']),
    ('Ingreso','Otros','Intereses / inversiones',['intereses','dividendos','rentabilidad']),
    ('Ingreso','Otros','Otros ingresos',['ingreso','cobro','venta']),
    ('Gasto','Tarjeta','Scotiabank Platinum',['scotiabank platinum','visa platinum']),
    ('Gasto','Tarjeta','Scotiabank Singular',['scotiabank singular','visa singular']),
    ('Gasto','Tarjeta','Falabella CMR',['falabella','cmr','saga']),
    ('Gasto','Fijo hogar','Terreno',['terreno','lote','parcela']),
    ('Gasto','Fijo hogar','Apoyo sobrino',['sobrino','apoyo']),
    ('Gasto','Fijo hogar','Telefonía',['claro','movistar','entel','telefonia','celular']),
    ('Gasto','Fijo hogar','Servicios Sthefanie',['servicio sthefanie','luz','agua','gas']),
    ('Gasto','Fijo hogar','Servicios Christian',['servicio christian']),
    ('Gasto','Variable','Supermercado / mercado',['metro','wong','tottus','plaza vea','makro','mercado','super']),
    ('Gasto','Variable','Transporte / taxi / Uber',['uber','taxi','bus','combi','pasaje','transporte','cabify','indriver']),
    ('Gasto','Variable','Gasolina',['gasolina','grifo','combustible','gasolinera']),
    ('Gasto','Variable','Salud / farmacia',['farmacia','inkafarma','mifarma','medicina','pastillas']),
    ('Gasto','Variable','Médico / clínica',['doctor','médico','clínica','hospital','consulta']),
    ('Gasto','Variable','Educación',['colegio','universidad','curso','academia']),
    ('Gasto','Variable','Higiene personal',['shampoo','jabón','higiene','desodorante']),
    ('Gasto','Variable','Ropa / calzado',['ropa','viale','zapatillas','calzado','zara','hm']),
    ('Gasto','No esencial','Restaurantes',['restaurante','polleria','cevichería','chifa','almuerzo','cena','comida']),
    ('Gasto','No esencial','Delivery',['rappi','pedidosya','delivery','domicilio']),
    ('Gasto','No esencial','Salidas / ocio',['salida','bar','antro','fiesta','cumple','karaoke']),
    ('Gasto','No esencial','Cine / entretenimiento',['cine','cinemark','cineplanet','pelicula','concierto']),
    ('Gasto','No esencial','Regalos',['regalo','obsequio','present']),
    ('Gasto','No esencial','Viajes',['hotel','vuelo','airbnb','hostal','viaje','vacaciones']),
    ('Gasto','No esencial','Suscripciones',['netflix','spotify','disney','amazon prime','youtube']),
    ('Gasto','No esencial','Gastos hormiga',['café','cafetería','dulce','snack']),
    ('Ahorro','Ahorros','Fondo de emergencia',['emergencia','fondo']),
    ('Ahorro','Ahorros','Meta ahorro (viajes)',['ahorro viaje','meta viaje']),
    ('Ahorro','Inversiones','Inversiones',['inversion','fondo mutuo','bolsa','acciones']),
    ('Ahorro','Ahorros','Otros ahorros',['ahorro','guardar']),
]

MEDIOS = ['efectivo','yape','plin','scotiabank platinum','scotiabank singular',
          'falabella','cmr','transferencia','débito','débito','visa']

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

def clasificar(texto: str) -> dict:
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

# ── EMOJIS por tipo ───────────────────────────────────────────────────────────
EMOJI = {
    'Ingreso': '🟢', 'Gasto': '🔴', 'Ahorro': '🔵',
    'No esencial': '🟣', 'Variable': '🟡', 'Fijo hogar': '🔴',
    'Tarjeta': '💳', 'Sueldo': '💼', 'Alquiler auto': '🚗',
}

def emoji_tipo(tipo, grupo):
    return EMOJI.get(grupo, EMOJI.get(tipo, '📝'))

# ── MENSAJE DE CONFIRMACIÓN ───────────────────────────────────────────────────
def build_confirm_msg(data: dict, quien: str) -> str:
    e = emoji_tipo(data.get('tipo',''), data.get('grupo',''))
    importe = f"S/ {data['importe']:,.2f}" if data.get('importe') else '❓ (no detecté el monto)'
    cuotas = f" · {data['cuotas']} cuotas" if data.get('cuotas') and data['cuotas'] != '-' else ''
    medio = data.get('medio', '—')
    lines = [
        f"📋 *Antes de guardar, confirma:*",
        f"",
        f"{e} *{data.get('descripcion','—')}*",
        f"📂 {data.get('tipo','—')} → {data.get('grupo','—')} → {data.get('categoria','—')}",
        f"💰 {importe}{cuotas}",
        f"💳 {medio}",
        f"👤 {quien}  ·  📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"",
    ]
    if data.get('duda'):
        lines.append(f"🤔 *Tengo una duda:* {data.get('pregunta','')}")
        lines.append(f"")
    lines.append(f"Responde *sí* para guardar, o corrígeme lo que esté mal.")
    return '\n'.join(lines)

# ── ESCRIBIR EN GOOGLE SHEETS ─────────────────────────────────────────────────
def guardar_en_sheet(data: dict, quien: str):
    now = datetime.now()
    sheet = get_sheet()
    row = [
        now.strftime('%d/%m/%Y'),          # A Fecha
        now.strftime('%H:%M'),              # B Hora
        data.get('descripcion', ''),        # C Descripción
        data.get('tipo', ''),               # D Tipo
        data.get('grupo', ''),              # E Grupo
        data.get('categoria', ''),          # F Categoría
        data.get('importe', 0),             # G Importe
        data.get('medio', ''),              # H Medio de pago
        data.get('cuotas', '-'),            # I Cuotas
        quien,                              # J Quién
        now.month,                          # K Mes
        data.get('notas', ''),              # L Notas
    ]
    sheet.append_row(row, value_input_option='USER_ENTERED')

# ── PARSING RÁPIDO DE CORRECCIONES ───────────────────────────────────────────
def aplicar_correccion(pending: dict, texto: str) -> dict:
    """Si el usuario dice 'el monto es 50' o 'es efectivo', corrige el pending."""
    t = texto.lower()
    # Monto
    m = re.search(r'(\d+[\.,]?\d*)', t)
    if m and any(w in t for w in ['monto','importe','son','fue','es']):
        pending['importe'] = float(m.group(1).replace(',','.'))
    # Medio
    for medio in ['efectivo','yape','plin','transferencia','débito','platinum','singular','cmr','falabella']:
        if medio in t:
            mapping = {
                'platinum': 'Scotiabank Platinum', 'singular': 'Scotiabank Singular',
                'falabella': 'Falabella CMR', 'cmr': 'Falabella CMR',
                'yape': 'Yape/Plin', 'plin': 'Yape/Plin',
                'efectivo': 'Efectivo', 'transferencia': 'Transferencia', 'débito': 'Débito',
            }
            pending['medio'] = mapping.get(medio, medio.capitalize())
    # Cuotas
    m2 = re.search(r'(\d+)\s*cuotas?', t)
    if m2:
        pending['cuotas'] = m2.group(1)
    return pending

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    from_num = request.form.get('From', '').replace('whatsapp:', '')
    body     = request.form.get('Body', '').strip()
    quien    = USUARIOS.get(from_num, 'Desconocido')

    if quien == 'Desconocido':
        reply('Número no autorizado. Contacta a Sthefanie o Christian.', from_num)
        return twiml_ok()

    sess = sessions.get(from_num, {})

    # ── FLUJO PRINCIPAL ────────────────────────────────────────────────────────

    # Comandos especiales
    if body.lower() in ['ayuda', 'help', '?']:
        msg = (
            "*Cómo registrar:*\n"
            "Escribe en lenguaje natural, por ejemplo:\n"
            "• `almuerzo pollería 45 efectivo`\n"
            "• `Rappi 35 yape`\n"
            "• `Scotiabank Platinum 4005 transferencia`\n"
            "• `sueldo mayo 5047`\n"
            "• `yape auto 350 ingreso`\n\n"
            "Siempre te pediré confirmación antes de guardar.\n"
            "Escribe *cancelar* para anular."
        )
        reply(msg, from_num)
        return twiml_ok()

    if body.lower() in ['cancelar', 'cancel', 'no']:
        sessions.pop(from_num, None)
        reply('❌ Registro cancelado. Cuando quieras, escribe tu gasto.', from_num)
        return twiml_ok()

    # ── Si hay pendiente esperando confirmación ────────────────────────────────
    if sess.get('step') == 'confirm':
        t = body.lower().strip()

        # Confirmación positiva
        if t in ['si','sí','yes','ok','dale','confirmar','guardar','correcto','bueno','ya']:
            try:
                guardar_en_sheet(sess['pending'], quien)
                imp = sess['pending'].get('importe', 0)
                tipo = sess['pending'].get('tipo', 'Gasto')
                emoji = '🟢' if tipo == 'Ingreso' else ('🔵' if tipo == 'Ahorro' else '🔴')
                msg = (
                    f"✅ *¡Guardado!*\n"
                    f"{emoji} {sess['pending'].get('descripcion','')} — "
                    f"S/ {imp:,.2f}\n"
                    f"_Registrado en Google Sheets_ 📊"
                )
                sessions.pop(from_num, None)
            except Exception as e:
                msg = f"⚠️ Error al guardar: {e}\nIntenta de nuevo."
            reply(msg, from_num)
            return twiml_ok()

        # Corrección parcial (no es ni sí ni no)
        else:
            sess['pending'] = aplicar_correccion(sess['pending'], body)
            sessions[from_num] = sess
            msg = build_confirm_msg(sess['pending'], quien)
            msg += '\n\n_Apliqué tu corrección. ¿Ahora sí está bien?_'
            reply(msg, from_num)
            return twiml_ok()

    # ── Nuevo mensaje → clasificar ─────────────────────────────────────────────
    data = clasificar(body)

    if 'error' in data:
        reply(f"⚠️ No pude procesar eso: {data['error']}\nIntenta de nuevo o escribe *ayuda*.", from_num)
        return twiml_ok()

    if not data.get('importe'):
        # Pedir el monto si no se detectó
        sessions[from_num] = {'step': 'confirm', 'pending': data}
        msg = (
            f"Entendí: *{data.get('descripcion','—')}*\n"
            f"Categoría: {data.get('categoria','—')}\n\n"
            f"🤔 No detecté el monto. ¿Cuánto fue?"
        )
        reply(msg, from_num)
        return twiml_ok()

    # Guardar pending y pedir confirmación
    sessions[from_num] = {'step': 'confirm', 'pending': data}
    reply(build_confirm_msg(data, quien), from_num)
    return twiml_ok()

def reply(msg: str, to: str):
    twilio.messages.create(
        body=msg,
        from_=TWILIO_WHATSAPP_NUM,
        to=f'whatsapp:{to}'
    )

def twiml_ok():
    return Response(str(MessagingResponse()), mimetype='text/xml')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
