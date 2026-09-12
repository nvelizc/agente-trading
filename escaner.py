"""
AGENTE DE INVESTIGACIÓN DE MERCADO 24/7 - v1 (escáner + señales)
-----------------------------------------------------------------
Esto NO es asesoría financiera. Es una herramienta que te ayuda a
detectar posibles operaciones para que VOS decidas si las ejecutás
en XTB. Nunca ejecuta órdenes automáticamente.

Qué hace, en orden:
1. Trae precio, variación y volumen de cada acción de tu watchlist.
2. Le manda esos datos a Claude junto con las reglas de setups.
3. Claude devuelve, en JSON, las señales que encuentra (o ninguna).
4. Filtra las señales de baja calidad (R:B menor a 1:1.5).
5. Imprime el resultado en pantalla y lo guarda en un log.

Requisitos:
    pip install yfinance anthropic

Variables de entorno necesarias:
    ANTHROPIC_API_KEY   -> tu clave de la API de Claude
"""

import os
import json
from datetime import datetime, timezone

import yfinance as yf
import anthropic

# -----------------------------------------------------------------
# 1. CONFIGURACIÓN - editá esto a tu gusto
# -----------------------------------------------------------------

# Watchlist: tickers de las acciones que querés seguir (formato Yahoo Finance)
WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]

# Ratio riesgo/beneficio mínimo para que una señal se considere válida
MIN_RATIO_RIESGO_BENEFICIO = 1.5

# Reglas de setups que el modelo va a usar para evaluar (los mismos
# conceptos del carrusel: ruptura, pullback, momentum, continuación, reversión)
REGLAS_SETUPS = """
- RUPTURA: el precio rompe un nivel clave de resistencia o soporte con volumen por encima del promedio.
- PULLBACK: retroceso temporal dentro de una tendencia clara, sin romper la estructura.
- MOMENTUM: aceleración fuerte del precio en una dirección, con volumen creciente.
- CONTINUACIÓN: la tendencia previa se mantiene después de una pausa o consolidación.
- REVERSIÓN: señales de giro de tendencia (ej. divergencias, rechazo en zona clave).
Si ninguno de estos patrones aparece con claridad, no inventes una señal: decí que no hay setup.
"""

# -----------------------------------------------------------------
# 2. TRAER DATOS DE MERCADO
# -----------------------------------------------------------------

def obtener_datos(ticker: str) -> dict:
    """Trae precio actual, variación del día, volumen y volumen promedio."""
    t = yf.Ticker(ticker)
    hist = t.history(period="30d")  # últimos 30 días para tener contexto de tendencia
    if hist.empty:
        return None

    precio_actual = hist["Close"].iloc[-1]
    precio_ayer = hist["Close"].iloc[-2]
    variacion_pct = ((precio_actual - precio_ayer) / precio_ayer) * 100

    volumen_hoy = hist["Volume"].iloc[-1]
    volumen_promedio = hist["Volume"].mean()

    return {
        "ticker": ticker,
        "precio_actual": round(float(precio_actual), 2),
        "variacion_pct": round(float(variacion_pct), 2),
        "volumen_hoy": int(volumen_hoy),
        "volumen_promedio_30d": int(volumen_promedio),
        "maximo_30d": round(float(hist["High"].max()), 2),
        "minimo_30d": round(float(hist["Low"].min()), 2),
        "precios_cierre_30d": [round(float(p), 2) for p in hist["Close"].tolist()],
    }


# -----------------------------------------------------------------
# 3. PEDIRLE A CLAUDE QUE EVALÚE LA SEÑAL
# -----------------------------------------------------------------

def evaluar_con_claude(client: anthropic.Anthropic, datos: dict) -> dict:
    """Le manda los datos de un ticker a Claude y le pide que evalúe si hay un setup."""

    prompt = f"""Sos un analista técnico que evalúa posibles operaciones para CFDs de acciones.
Datos de {datos['ticker']}:
- Precio actual: {datos['precio_actual']}
- Variación de hoy: {datos['variacion_pct']}%
- Volumen hoy: {datos['volumen_hoy']} (promedio 30d: {datos['volumen_promedio_30d']})
- Máximo 30d: {datos['maximo_30d']} / Mínimo 30d: {datos['minimo_30d']}
- Serie de precios de cierre (últimos 30 días, orden cronológico): {datos['precios_cierre_30d']}

Reglas de setups a aplicar:
{REGLAS_SETUPS}

Evaluá si hay un setup de alta probabilidad. Respondé SOLO con un JSON (sin texto
adicional, sin markdown) con esta forma exacta:

{{
  "hay_señal": true o false,
  "tipo_setup": "ruptura|pullback|momentum|continuacion|reversion|null",
  "direccion": "long|short|null",
  "confianza": "alta|media|baja|null",
  "entrada": numero o null,
  "stop_loss": numero o null,
  "objetivo": numero o null,
  "ratio_riesgo_beneficio": numero o null,
  "razon": "explicación breve en español, en lenguaje simple para alguien que no sabe de trading"
}}
"""

    respuesta = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    texto = respuesta.content[0].text.strip()
    # Por si el modelo agrega ```json ... ``` alrededor
    texto = texto.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return {"hay_señal": False, "razon": "No se pudo interpretar la respuesta del modelo."}


# -----------------------------------------------------------------
# 4. FILTRAR Y GUARDAR RESULTADOS
# -----------------------------------------------------------------

def pasa_filtro_de_calidad(señal: dict) -> bool:
    if not señal.get("hay_señal"):
        return False
    if señal.get("confianza") != "alta":
        return False
    ratio = señal.get("ratio_riesgo_beneficio")
    if ratio is None or ratio < MIN_RATIO_RIESGO_BENEFICIO:
        return False
    return True


def guardar_log(resultados: list):
    os.makedirs("logs", exist_ok=True)

    momento = datetime.now(timezone.utc)

    # 1) Guarda un archivo histórico con timestamp (para no perder nada)
    nombre_historico = f"logs/escaneo_{momento.strftime('%Y%m%d_%H%M')}.json"
    with open(nombre_historico, "w", encoding="utf-8") as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)

    # 2) Sobrescribe SIEMPRE logs/latest.json -> esto es lo que va a leer el dashboard
    paquete = {
        "actualizado": momento.isoformat(),
        "resultados": resultados,
    }
    with open("logs/latest.json", "w", encoding="utf-8") as f:
        json.dump(paquete, f, ensure_ascii=False, indent=2)

    print(f"\nLog guardado en: {nombre_historico}")
    print("Archivo logs/latest.json actualizado (esto lo lee el dashboard).")


# -----------------------------------------------------------------
# 5. PROGRAMA PRINCIPAL
# -----------------------------------------------------------------

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Falta la variable de entorno ANTHROPIC_API_KEY")

    client = anthropic.Anthropic(api_key=api_key)

    print(f"Escaneando {len(WATCHLIST)} acciones... ({datetime.now(timezone.utc).isoformat()})\n")

    resultados = []
    señales_validas = []

    for ticker in WATCHLIST:
        datos = obtener_datos(ticker)
        if datos is None:
            print(f"[{ticker}] No se pudieron obtener datos.")
            continue

        señal = evaluar_con_claude(client, datos)
        señal["ticker"] = ticker
        señal["precio_al_momento_del_analisis"] = datos["precio_actual"]
        resultados.append(señal)

        if pasa_filtro_de_calidad(señal):
            señales_validas.append(señal)
            print(f"[{ticker}] ✅ SEÑAL: {señal['tipo_setup']} ({señal['direccion']}) "
                  f"- Entrada: {señal['entrada']} / Stop: {señal['stop_loss']} / "
                  f"Objetivo: {señal['objetivo']} / R:B 1:{señal['ratio_riesgo_beneficio']}")
        else:
            print(f"[{ticker}] Sin señal de alta calidad por ahora.")

    guardar_log(resultados)

    print(f"\nTotal de señales de alta calidad encontradas: {len(señales_validas)}")
    return señales_validas


if __name__ == "__main__":
    main()
