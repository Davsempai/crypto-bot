"""
utils/log_reader.py — Lee el log del bot y lo formatea para Telegram

Permite enviar los últimos N líneas del log directamente por Telegram,
filtrando por nivel (ERROR, WARNING, INFO) para fácil diagnóstico.
"""
import os
from datetime import datetime, timezone
from utils.logger import setup_logger

logger = setup_logger("log_reader")

LOG_PATH = "data/bot.log"


def read_last_lines(n: int = 50) -> list[str]:
    """Lee las últimas N líneas del archivo de log."""
    if not os.path.exists(LOG_PATH):
        return ["Log file no encontrado — ¿el bot acaba de arrancar?"]
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:] if l.strip()]
    except Exception as e:
        return [f"Error leyendo log: {e}"]


def filter_errors(lines: list[str]) -> list[str]:
    """Filtra solo líneas de ERROR y WARNING."""
    return [l for l in lines if "[ERROR]" in l or "[WARNING]" in l]


def format_log_for_telegram(lines: list[str], title: str = "LOG") -> list[str]:
    """
    Divide el log en chunks de máximo 3800 chars para Telegram.
    Retorna lista de mensajes listos para enviar.
    """
    if not lines:
        return ["📋 Sin entradas en el log."]

    chunks = []
    current = f"📋 *{title}*\n```\n"
    for line in lines:
        # Acortar líneas muy largas
        short = line[:200] + "…" if len(line) > 200 else line
        if len(current) + len(short) + 10 > 3800:
            current += "```"
            chunks.append(current)
            current = "```\n"
        current += short + "\n"
    if current.strip("```\n"):
        current += "```"
        chunks.append(current)
    return chunks


def get_summary() -> str:
    """Resumen rápido de errores recientes."""
    lines   = read_last_lines(200)
    errors  = [l for l in lines if "[ERROR]" in l]
    warns   = [l for l in lines if "[WARNING]" in l]
    infos   = [l for l in lines if "[INFO]" in l]

    # Contar errores únicos
    error_types: dict[str, int] = {}
    for l in errors:
        # Extraer módulo del error
        try:
            mod = l.split("[ERROR]")[1].split("—")[0].strip()
            error_types[mod] = error_types.get(mod, 0) + 1
        except Exception:
            pass

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    text = f"📋 *RESUMEN DEL LOG* — `{ts}`\n"
    text += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"📊 Últimas 200 líneas:\n"
    text += f"  ✅ INFO:    `{len(infos)}`\n"
    text += f"  ⚠️ WARNING: `{len(warns)}`\n"
    text += f"  ❌ ERROR:   `{len(errors)}`\n"

    if error_types:
        text += "\n*Errores por módulo:*\n"
        for mod, cnt in sorted(error_types.items(), key=lambda x: -x[1])[:8]:
            text += f"  • `{mod}`: {cnt}×\n"

    if errors:
        text += "\n*Último error:*\n"
        text += f"```\n{errors[-1][:300]}\n```"

    return text
