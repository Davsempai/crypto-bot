# 🤖 Crypto Futures Bot — Telegram

Bot de señales para trading de **futuros crypto** con análisis técnico avanzado e inteligencia artificial para análisis macro.

---

## ✨ Funcionalidades

### 📊 Análisis Técnico
- Detección de estructura de mercado (BOS / CHoCH)
- Identificación de barridos de liquidez
- Fair Value Gaps (FVG) como zonas de entrada
- Order Blocks (OB) como zonas de soporte/resistencia
- RSI, EMA, MACD, ATR, Bollinger Bands

### 📡 Datos de Futuros (Binance)
- Funding Rate en tiempo real con alertas
- Open Interest y cambios porcentuales
- Long/Short Ratio
- Precio, volumen y stats 24h

### 🤖 Análisis Macro con IA (Claude)
- Monitoreo de noticias de CryptoPanic
- Clasificación automática: BULLISH / BEARISH / NEUTRAL
- Impacto: HIGH / MEDIUM / LOW
- Recomendación de trading contextual
- Resumen ejecutivo de mercado cada 4 horas

### 📲 Señales con Gestión de Riesgo
- Zona de entrada (FVG u OB más cercano)
- Stop Loss basado en ATR
- TP1 (1:1.5R) y TP2 (1:3R)
- Confianza basada en confluencias (mínimo 3/7)
- Track record persistente en SQLite

---

## 🚀 Instalación

### 1. Clonar y preparar entorno

```bash
git clone <repo>
cd crypto_futures_bot
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

```bash
cp .env.example .env
# Edita .env con tus claves
```

### 3. Obtener las API Keys

#### 🤖 Telegram Bot Token
1. Abre Telegram y busca `@BotFather`
2. Escribe `/newbot`
3. Sigue las instrucciones y copia el token
4. Para obtener tu user ID: busca `@userinfobot` y escribe `/start`

#### 📊 Binance API
1. Ve a [binance.com](https://www.binance.com) → Perfil → API Management
2. Crea una API key
3. Habilita **Futures Trading** (lectura solamente para empezar)
4. Copia API Key y Secret

#### 🧠 Anthropic (Claude AI)
1. Ve a [console.anthropic.com](https://console.anthropic.com)
2. API Keys → Create Key
3. Copia la clave

#### 📰 CryptoPanic (Noticias)
1. Ve a [cryptopanic.com/developers/api](https://cryptopanic.com/developers/api/)
2. Regístrate gratis
3. Copia tu API token

### 4. Configurar el canal de señales

```bash
# 1. Crea un canal en Telegram (o grupo)
# 2. Agrega tu bot como administrador
# 3. Obtén el ID del canal:
#    - Forwarda un mensaje del canal a @userinfobot
#    - El ID es el número negativo (ej: -1001234567890)
# 4. Ponlo en SIGNAL_CHANNEL_ID en el .env
```

### 5. Ejecutar

```bash
python main.py
```

---

## 📋 Comandos del Bot

| Comando | Descripción |
|---------|-------------|
| `/start` | Menú principal interactivo |
| `/market` | Resumen de mercado con IA |
| `/analyze [PAR]` | Análisis técnico de un par |
| `/signals` | Ver señales abiertas |
| `/news` | Últimas noticias importantes |
| `/funding` | Funding rates actuales |
| `/stats` | Estadísticas del bot |
| `/scan` | Escanear todos los pares |
| `/help` | Ayuda |

---

## 📐 Estrategia Técnica

```
CONFLUENCIAS REQUERIDAS (mínimo 3 de 7):

LONG:
  ✅ Estructura alcista (HH/HL)
  ✅ CHoCH alcista confirmado
  ✅ Barrido de liquidez bearish (shortsqueeze)
  ✅ RSI en sobreventa
  ✅ Funding negativo (mercado sobre-corto)
  ✅ OI creciendo con estructura alcista
  ✅ Volumen elevado

SHORT: (inverso de los anteriores)
```

---

## ⚙️ Configuración Avanzada

```env
# Pares a monitorear
TRADING_PAIRS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT

# Umbral de funding rate para alertas (0.05 = 0.05%)
FUNDING_RATE_THRESHOLD=0.05

# Cambio de OI en 1h para alertas (5 = 5%)
OI_CHANGE_THRESHOLD=5.0

# Riesgo por operación (1 = 1% del capital)
MAX_RISK_PER_TRADE=1.0

# Intervalo de chequeo en segundos
CHECK_INTERVAL=60

# Modo paper trading (true = análisis sin capital real)
PAPER_TRADING=true
```

---

## 📁 Estructura del Proyecto

```
crypto_futures_bot/
├── main.py                    # Punto de entrada
├── config.py                  # Configuración central
├── requirements.txt
├── .env.example
├── data/                      # Base de datos y logs (auto-generado)
│   ├── bot_database.db
│   └── bot.log
├── bot/
│   └── telegram_bot.py        # Bot + comandos + monitor
├── market/
│   └── binance_client.py      # Cliente Binance Futures
├── analysis/
│   ├── technical.py           # Motor técnico (BOS, FVG, OB, RSI...)
│   └── macro.py               # Análisis macro con Claude AI
├── alerts/
│   └── alert_manager.py       # Formato de mensajes Telegram
└── utils/
    ├── database.py             # SQLite (señales, stats, noticias)
    └── logger.py               # Logging con colores
```

---

## ⚠️ Disclaimer

Este bot es una **herramienta de análisis**, NO garantiza resultados.
- Siempre gestiona tu riesgo (máximo 1-2% por operación)
- El trading de futuros conlleva riesgo de pérdida total
- Usa **PAPER_TRADING=true** para validar antes de operar real
- El bot puede equivocarse: el mercado siempre tiene la última palabra

---

## 🗺️ Roadmap

- [x] **Fase 1** — MVP: Alertas + Análisis básico + Telegram
- [ ] **Fase 2** — Motor técnico avanzado + Multi-timeframe
- [ ] **Fase 3** — Dashboard web + Backtesting automático
- [ ] **Fase 4** — Ejecución automática (con confirmación manual)
