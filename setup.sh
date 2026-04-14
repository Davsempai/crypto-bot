#!/usr/bin/env bash
# =============================================================================
# setup.sh — Instalador automático del Crypto Futures Bot v4
# =============================================================================
# Uso:
#   chmod +x setup.sh
#   ./setup.sh
# =============================================================================

set -euo pipefail

# ── Colores ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}  ✅ $1${NC}"; }
fail() { echo -e "${RED}  ❌ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠️  $1${NC}"; }
info() { echo -e "${CYAN}  ℹ️  $1${NC}"; }
step() { echo -e "\n${BOLD}${BLUE}▶ $1${NC}"; }
sep()  { echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Banner ────────────────────────────────────────────────────────────────────
clear
sep
echo -e "${BOLD}${CYAN}"
echo "   🤖  CRYPTO FUTURES BOT v4"
echo "       Setup & Verificación automática"
echo -e "${NC}"
sep

ERRORS=0
WARNINGS=0

# ── Paso 1: Python ─────────────────────────────────────────────────────────
step "Verificando Python"

if ! command -v python3 &>/dev/null; then
    fail "Python 3 no encontrado. Instala Python 3.10 o superior."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Se requiere Python 3.10+. Encontrado: $PY_VERSION"
    exit 1
fi

ok "Python $PY_VERSION"

# ── Paso 2: Entorno virtual ───────────────────────────────────────────────
step "Entorno virtual"

if [ ! -d "venv" ]; then
    info "Creando entorno virtual..."
    python3 -m venv venv
    ok "Entorno virtual creado"
else
    ok "Entorno virtual ya existe"
fi

# Activar
source venv/bin/activate || { fail "No se pudo activar el entorno virtual"; exit 1; }
ok "Entorno virtual activado"

# ── Paso 3: Dependencias ──────────────────────────────────────────────────
step "Instalando dependencias"

pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

ok "Dependencias instaladas"

# ── Paso 4: Archivo .env ──────────────────────────────────────────────────
step "Configuración (.env)"

if [ ! -f ".env" ]; then
    cp .env.example .env
    warn ".env creado desde plantilla — DEBES completarlo antes de arrancar"
    warn "Abre .env con tu editor y rellena las credenciales"
    WARNINGS=$((WARNINGS + 1))
else
    ok ".env ya existe"
fi

# ── Paso 5: Directorios necesarios ───────────────────────────────────────
step "Creando directorios"

mkdir -p data dashboard/static
ok "data/ y dashboard/static/ listos"

# ── Paso 6: Leer variables del .env ──────────────────────────────────────
step "Leyendo configuración"

# Cargar .env sin exportar (portable)
while IFS='=' read -r key val; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    val="${val%%#*}"     # quitar comentarios inline
    val="${val//\"/}"   # quitar comillas
    val="${val// /}"    # quitar espacios
    export "$key=$val"
done < .env

EXCHANGE="${EXCHANGE:-weex}"
ok "Exchange configurado: $EXCHANGE"

# ── Paso 7: Verificar credenciales ───────────────────────────────────────
step "Verificando credenciales"

# Telegram
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [[ "$TELEGRAM_BOT_TOKEN" == *"xxxxxxxxx"* ]]; then
    fail "TELEGRAM_BOT_TOKEN no configurado en .env"
    ERRORS=$((ERRORS + 1))
else
    ok "TELEGRAM_BOT_TOKEN presente"
fi

if [ -z "${AUTHORIZED_USERS:-}" ] || [[ "$AUTHORIZED_USERS" == "123456789" ]]; then
    warn "AUTHORIZED_USERS parece ser el valor de ejemplo — verifica tu user ID"
    WARNINGS=$((WARNINGS + 1))
else
    ok "AUTHORIZED_USERS configurado"
fi

# Exchange
if [ "$EXCHANGE" = "weex" ]; then
    if [ -z "${WEEX_API_KEY:-}" ] || [[ "$WEEX_API_KEY" == *"xxx"* ]]; then
        fail "WEEX_API_KEY no configurado"
        ERRORS=$((ERRORS + 1))
    else
        ok "WEEX_API_KEY presente"
    fi

    if [ -z "${WEEX_SECRET_KEY:-}" ] || [[ "$WEEX_SECRET_KEY" == *"xxx"* ]]; then
        fail "WEEX_SECRET_KEY no configurado"
        ERRORS=$((ERRORS + 1))
    else
        ok "WEEX_SECRET_KEY presente"
    fi

    if [ -z "${WEEX_PASSPHRASE:-}" ] || [[ "$WEEX_PASSPHRASE" == *"passphrase"* ]]; then
        fail "WEEX_PASSPHRASE no configurado"
        ERRORS=$((ERRORS + 1))
    else
        ok "WEEX_PASSPHRASE presente"
    fi
else
    if [ -z "${BINANCE_API_KEY:-}" ] || [[ "$BINANCE_API_KEY" == *"xxx"* ]]; then
        fail "BINANCE_API_KEY no configurado"
        ERRORS=$((ERRORS + 1))
    else
        ok "BINANCE_API_KEY presente"
    fi
fi

# Anthropic
if [ -z "${ANTHROPIC_API_KEY:-}" ] || [[ "$ANTHROPIC_API_KEY" == *"xxx"* ]]; then
    warn "ANTHROPIC_API_KEY no configurado — análisis macro con IA desactivado"
    WARNINGS=$((WARNINGS + 1))
else
    ok "ANTHROPIC_API_KEY presente"
fi

# CryptoPanic
    WARNINGS=$((WARNINGS + 1))
else
fi

# ── Paso 8: Probar conexión a la API del exchange ─────────────────────────
step "Probando conexión al exchange ($EXCHANGE)"

if [ "$EXCHANGE" = "weex" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        "https://api-contract.weex.com/capi/v3/market/time" \
        --max-time 10 2>/dev/null || echo "000")

    if [ "$HTTP_CODE" = "200" ]; then
        ok "Conexión a Weex API: OK (HTTP $HTTP_CODE)"
    else
        fail "No se pudo conectar a Weex API (HTTP $HTTP_CODE) — verifica tu internet"
        ERRORS=$((ERRORS + 1))
    fi

    # Probar endpoint de precio BTC
    BTC_DATA=$(curl -s "https://api-contract.weex.com/capi/v3/market/symbolPrice?symbol=BTCUSDT" \
        --max-time 10 2>/dev/null || echo "{}")
    BTC_PRICE=$(echo "$BTC_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('price','0'))" 2>/dev/null || echo "0")

    if [ "$BTC_PRICE" != "0" ] && [ -n "$BTC_PRICE" ]; then
        ok "Precio BTC/USDT en Weex: \$$BTC_PRICE"
    else
        warn "No se pudo obtener precio de BTC (el endpoint de precios puede requerir auth)"
        WARNINGS=$((WARNINGS + 1))
    fi
else
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        "https://fapi.binance.com/fapi/v1/time" \
        --max-time 10 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        ok "Conexión a Binance API: OK"
    else
        fail "No se pudo conectar a Binance API (HTTP $HTTP_CODE)"
        ERRORS=$((ERRORS + 1))
    fi
fi

# ── Paso 9: Probar conexión a Telegram ───────────────────────────────────
step "Probando Telegram Bot"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [[ "$TELEGRAM_BOT_TOKEN" != *"xxxxxxxxx"* ]]; then
    TG_RESPONSE=$(curl -s \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" \
        --max-time 10 2>/dev/null || echo '{"ok":false}')

    TG_OK=$(echo "$TG_RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d.get('ok','false'))" 2>/dev/null || echo "false")
    BOT_NAME=$(echo "$TG_RESPONSE" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); r=d.get('result',{}); print(r.get('username','?'))" 2>/dev/null || echo "?")

    if [ "$TG_OK" = "True" ] || [ "$TG_OK" = "true" ]; then
        ok "Telegram Bot conectado: @$BOT_NAME"
    else
        fail "Telegram Bot token inválido — verifica TELEGRAM_BOT_TOKEN"
        ERRORS=$((ERRORS + 1))
    fi
else
    info "Omitiendo prueba de Telegram (token no configurado)"
fi

# ── Paso 10: Verificar sintaxis del proyecto ──────────────────────────────
step "Verificando código Python"

python3 -c "
import ast, os
errors = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('__pycache__', 'venv', '.git')]
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            try:
                ast.parse(open(path).read())
            except SyntaxError as e:
                errors.append(f'{path}: {e}')
if errors:
    for e in errors:
        print(f'SYNTAX ERROR: {e}')
    exit(1)
else:
    print('OK')
" && ok "Sintaxis de todos los archivos .py: OK" || { fail "Errores de sintaxis encontrados"; ERRORS=$((ERRORS + 1)); }

# ── Resultado final ───────────────────────────────────────────────────────
sep
echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo -e "${RED}${BOLD}  RESULTADO: $ERRORS error(es) encontrado(s)${NC}"
    echo -e "${YELLOW}  Corrige los errores marcados con ❌ antes de arrancar.${NC}"
elif [ "$WARNINGS" -gt 0 ]; then
    echo -e "${YELLOW}${BOLD}  RESULTADO: Listo con $WARNINGS advertencia(s)${NC}"
    echo -e "${CYAN}  Revisa las advertencias ⚠️  — el bot puede arrancar pero con funciones limitadas.${NC}"
else
    echo -e "${GREEN}${BOLD}  RESULTADO: ✅ Todo correcto — listo para arrancar${NC}"
fi

echo ""
sep
echo -e "${BOLD}  CÓMO ARRANCAR:${NC}"
echo ""
echo -e "  ${CYAN}# Activar entorno (si no lo está)${NC}"
echo -e "  source venv/bin/activate"
echo ""
echo -e "  ${CYAN}# Bot + Dashboard juntos (recomendado)${NC}"
echo -e "  python main.py"
echo ""
echo -e "  ${CYAN}# Solo el dashboard web (sin Telegram)${NC}"
echo -e "  python main.py --mode dashboard --port 8080"
echo ""
echo -e "  ${CYAN}# Solo el bot de Telegram${NC}"
echo -e "  python main.py --mode bot"
echo ""
echo -e "  ${CYAN}# Dashboard en otro puerto${NC}"
echo -e "  python main.py --port 3000"
echo ""
sep
echo -e "  ${BOLD}Dashboard:${NC}  http://localhost:8080"
echo -e "  ${BOLD}Telegram:${NC}   Escribe /start a tu bot"
sep
echo ""

[ "$ERRORS" -gt 0 ] && exit 1 || exit 0
