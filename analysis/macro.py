"""
analysis/macro.py — Análisis macro con múltiples fuentes gratuitas

Fuentes de noticias (sin API key, sin costo):
  Principal : free-crypto-news.vercel.app
    /api/analyze  → noticias con sentimiento ML ya calculado
    /api/trending → tópicos trending con sentimiento de mercado
    /api/breaking → noticias de las últimas 2 horas
    /api/search   → búsqueda por palabra clave

  Espejo    : nirholas.github.io/free-crypto-news/cache/latest.json
    (respaldo automático si el principal cae)

  RSS       : CoinDesk, Cointelegraph, Decrypt
    (último recurso si ambas APIs fallan)

Análisis de sentimiento:
  - Si hay ANTHROPIC_API_KEY → Claude Haiku (más barato)
  - Si no                    → análisis por reglas ponderadas (gratis)
"""
import asyncio
import re
import aiohttp
import json
from datetime import datetime, timezone
from typing import Optional

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from config import config
from utils.logger import setup_logger

logger = setup_logger("macro")

# IDs de noticias ya procesadas (evita duplicados entre ciclos)
_processed_news_ids: set = set()

# ─── URLs de la API ───────────────────────────────────────────────────────────
FCN_BASE     = "https://free-crypto-news.vercel.app"
FCN_FAILSAFE = "https://nirholas.github.io/free-crypto-news/cache/latest.json"
FCN_HEADERS  = {"User-Agent": "CryptoFuturesBot/4.0", "Accept": "application/json"}

RSS_SOURCES = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
]

HIGH_IMPACT_KEYWORDS = [
    "fed", "federal reserve", "fomc", "cpi", "inflation", "rate hike", "rate cut",
    "sec", "cftc", "regulation", "ban", "lawsuit", "etf approved", "etf approval",
    "hack", "exploit", "rug pull", "flash crash", "liquidation", "liquidat",
    "blackrock", "fidelity", "grayscale", "microstrategy", "institutional",
    "halving", "all-time high", "ath", "short squeeze", "long squeeze",
    "billion", "crash", "surge", "bitcoin", "ethereum", "solana",
]


# ─── MacroAnalyzer ────────────────────────────────────────────────────────────

class MacroAnalyzer:
    """
    Analiza sentimiento de noticias.
    Usa Claude Haiku si hay API key, análisis por reglas si no.
    """

    CLAUDE_PROMPT = """Eres un analista experto en mercados de futuros crypto.
Analiza la noticia y responde SOLO en JSON con este formato exacto (sin markdown):
{
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
  "impact": "HIGH" | "MEDIUM" | "LOW",
  "affected_assets": ["BTC", "ETH", "SOL"],
  "reasoning": "Explicación de 1-2 oraciones en español",
  "trading_advice": "AVOID_NEW_LONGS" | "AVOID_NEW_SHORTS" | "WAIT" | "OPPORTUNITY_LONG" | "OPPORTUNITY_SHORT" | "NORMAL",
  "time_horizon": "IMMEDIATE" | "SHORT_TERM" | "MEDIUM_TERM"
}"""

    def __init__(self):
        self._claude      = None
        self._claude_dead = False   # True si el balance está agotado → no reintentar
        if _ANTHROPIC_AVAILABLE and config.ANTHROPIC_API_KEY:
            try:
                import anthropic as _ant
                self._claude = _ant.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
                logger.info("✅ Análisis macro: Claude Haiku activo")
            except Exception as e:
                logger.warning(f"Claude init error: {e} — usando análisis por reglas")
        else:
            logger.info("ℹ️  Análisis macro: modo reglas (gratis, sin API key)")

    async def analyze_news(self, title: str, content: str = "",
                           pre_sentiment: str = "") -> dict:
        """
        Analiza una noticia. Orden de prioridad:
          1. Sentimiento ya dado por la API (pre_sentiment) → mapear directamente
          2. Claude Haiku si hay API key y tiene crédito
          3. Análisis por reglas ponderadas
        """
        # 1. Usar sentimiento pre-calculado si viene de la API
        if pre_sentiment and pre_sentiment.lower() in ("bullish", "bearish", "neutral"):
            return self._from_prescore(title, content, pre_sentiment)

        # 2. Claude si está disponible y tiene crédito
        if self._claude and not self._claude_dead:
            return await self._claude_analyze(title, content)

        # 3. Reglas
        return self._rules_analyze(title, content)

    def _from_prescore(self, title: str, content: str, sentiment_raw: str) -> dict:
        """Construye el resultado a partir del sentimiento ya calculado por ML."""
        sentiment = sentiment_raw.upper()   # bullish → BULLISH

        # Determinar impacto por keywords
        text = (title + " " + content).lower()
        is_high = any(kw in text for kw in HIGH_IMPACT_KEYWORDS)
        impact = "HIGH" if is_high else "MEDIUM"

        # Activos afectados
        affected = []
        if any(w in text for w in ["bitcoin", "btc"]): affected.append("BTC")
        if any(w in text for w in ["ethereum", "eth"]): affected.append("ETH")
        if any(w in text for w in ["solana", "sol"]): affected.append("SOL")
        if not affected: affected = ["BTC", "ETH"]

        advice_map = {
            "BULLISH":  "OPPORTUNITY_LONG"  if impact == "HIGH" else "NORMAL",
            "BEARISH":  "AVOID_NEW_LONGS"   if impact == "HIGH" else "WAIT",
            "NEUTRAL":  "NORMAL",
        }

        return {
            "sentiment":      sentiment,
            "impact":         impact,
            "affected_assets": affected,
            "reasoning":      f"Sentimiento {sentiment.lower()} detectado por ML. {title[:80]}",
            "trading_advice": advice_map.get(sentiment, "NORMAL"),
            "time_horizon":   "SHORT_TERM",
        }

    async def _claude_analyze(self, title: str, content: str) -> dict:
        """Análisis con Claude Haiku (modelo barato)."""
        try:
            text = f"Noticia: {title}"
            if content:
                text += f"\n\nDetalle: {content[:400]}"

            msg = await self._claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=350,
                system=self.CLAUDE_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"```(?:json)?", "", raw).strip()
            result = json.loads(raw)
            logger.debug(f"Claude → {result.get('sentiment')} / {result.get('impact')}")
            return result

        except Exception as e:
            err_str = str(e)
            # Balance agotado → desactivar Claude permanentemente en esta sesión
            if "credit balance is too low" in err_str or "402" in err_str:
                logger.warning("💳 Balance de Anthropic agotado — cambiando a análisis por reglas permanentemente")
                self._claude_dead = True
            else:
                logger.warning(f"Claude error: {e} — usando reglas")
            return self._rules_analyze(title, content)

    def _rules_analyze(self, title: str, content: str = "") -> dict:
        """Análisis por palabras clave ponderadas. 100% gratis y offline."""
        text = (title + " " + content).lower()

        BULLISH_HIGH = [
            "etf approved", "sec approves", "all-time high", "ath",
            "rate cut", "fed pivot", "short squeeze", "halving",
            "institutional buy", "blackrock buys", "billion investment",
        ]
        BULLISH_MID = [
            "surge", "rally", "pump", "gains", "bullish", "adoption",
            "upgrade", "mainnet", "launch", "partnership", "etf",
        ]
        BEARISH_HIGH = [
            "hack", "exploit", "rug pull", "sec sues", "ban",
            "crash", "collapse", "bankrupt", "long squeeze", "exchange down",
        ]
        BEARISH_MID = [
            "dump", "drop", "bearish", "regulation", "lawsuit",
            "rate hike", "inflation", "cpi higher", "fine", "penalty",
        ]

        bull = sum(2 for k in BULLISH_HIGH if k in text) + sum(1 for k in BULLISH_MID if k in text)
        bear = sum(2 for k in BEARISH_HIGH if k in text) + sum(1 for k in BEARISH_MID if k in text)

        if bull > bear + 1:   sentiment = "BULLISH"
        elif bear > bull + 1: sentiment = "BEARISH"
        else:                  sentiment = "NEUTRAL"

        is_high = any(kw in text for kw in HIGH_IMPACT_KEYWORDS)
        total   = bull + bear
        impact  = "HIGH" if (is_high and total >= 3) else ("MEDIUM" if total >= 2 else "LOW")

        affected = []
        if any(w in text for w in ["bitcoin", "btc"]): affected.append("BTC")
        if any(w in text for w in ["ethereum", "eth"]): affected.append("ETH")
        if any(w in text for w in ["solana", "sol"]): affected.append("SOL")
        if not affected: affected = ["BTC", "ETH"]

        advice_map = {
            ("BULLISH", "HIGH"):   "OPPORTUNITY_LONG",
            ("BULLISH", "MEDIUM"): "NORMAL",
            ("BEARISH", "HIGH"):   "AVOID_NEW_LONGS",
            ("BEARISH", "MEDIUM"): "WAIT",
            ("NEUTRAL", "HIGH"):   "WAIT",
        }
        advice = advice_map.get((sentiment, impact), "NORMAL")

        return {
            "sentiment":      sentiment,
            "impact":         impact,
            "affected_assets": affected,
            "reasoning":      f"Análisis por reglas: {bull} señales alcistas, {bear} bajistas en el título.",
            "trading_advice": advice,
            "time_horizon":   "SHORT_TERM",
        }

    async def generate_market_summary(self, market_data_list: list[dict]) -> str:
        """Resumen de mercado. Claude si hay key y crédito, reglas si no."""
        if not market_data_list:
            return "Sin datos de mercado disponibles."

        if self._claude and not self._claude_dead:
            try:
                rows = "\n".join(
                    f"- {d['pair']}: ${d['price']:,.2f} | "
                    f"Funding: {d['funding_rate']:+.4f}% | "
                    f"OI 1h: {d['oi_change_1h']:+.1f}% | "
                    f"24h: {d['price_change_24h']:+.1f}%"
                    for d in market_data_list
                )
                msg = await self._claude.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=300,
                    messages=[{"role": "user", "content":
                        f"Analiza el mercado de futuros crypto y dame un resumen ejecutivo "
                        f"en español de máximo 80 palabras. Sesgo general, alertas clave y qué vigilar.\n\n{rows}"
                    }],
                )
                return msg.content[0].text.strip()
            except Exception as e:
                if "credit balance is too low" in str(e) or "402" in str(e):
                    logger.warning("💳 Balance Anthropic agotado — desactivando Claude")
                    self._claude_dead = True
                else:
                    logger.warning(f"Claude summary error: {e}")

        # Fallback por reglas
        avg_chg = sum(d.get("price_change_24h", 0) for d in market_data_list) / len(market_data_list)
        sesgo   = "alcista" if avg_chg > 1 else "bajista" if avg_chg < -1 else "neutral"

        alerts = []
        for d in market_data_list:
            fr  = d.get("funding_rate", 0)
            oi  = d.get("oi_change_1h", 0)
            if abs(fr) > 0.05:
                lbl = "sobre-largo" if fr > 0 else "sobre-corto"
                alerts.append(f"{d['pair']} funding {lbl} ({fr:+.4f}%)")
            if abs(oi) > 5:
                alerts.append(f"{d['pair']} OI {oi:+.1f}% en 1h")

        summary = f"Mercado con sesgo {sesgo} (24h prom. {avg_chg:+.1f}%)."
        if alerts:
            summary += f" Alertas: {'; '.join(alerts[:3])}."
        summary += " Gestión de riesgo estricta recomendada."
        return summary


# ─── NewsMonitor ─────────────────────────────────────────────────────────────

class NewsMonitor:
    """
    Obtiene noticias crypto de múltiples fuentes gratuitas.

    Prioridad:
      1. free-crypto-news /api/analyze  → sentimiento ML incluido
      2. free-crypto-news /api/breaking → noticias < 2h
      3. free-crypto-news /api/trending → tópicos trending
      4. Espejo GitHub Pages           → si la API principal cae
      5. RSS feeds                     → último recurso
    """

    def __init__(self, analyzer: MacroAnalyzer):
        self.analyzer = analyzer

    # ─── Fuente principal: free-crypto-news ───────────────────────────────────

    async def _fetch_fcn(self, endpoint: str, params: dict = None) -> list[dict]:
        """Llama a un endpoint de free-crypto-news.vercel.app."""
        try:
            url = f"{FCN_BASE}{endpoint}"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params or {},
                                 headers=FCN_HEADERS,
                                 timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)

            articles = data.get("articles", data) if isinstance(data, dict) else data
            return articles if isinstance(articles, list) else []
        except Exception as e:
            logger.debug(f"FCN {endpoint} error: {e}")
            return []

    async def _fetch_analyzed(self) -> list[dict]:
        """
        /api/analyze — noticias con topic classification y sentimiento ML.
        Retorna artículos normalizados con campo 'pre_sentiment'.
        """
        raw = await self._fetch_fcn("/api/analyze", {"limit": 30})
        items = []
        for a in raw:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            # La API puede incluir sentiment directamente o como campo aparte
            sentiment_raw = (
                a.get("sentiment") or
                a.get("sentimentLabel") or
                ""
            )
            items.append({
                "id":            a.get("link") or a.get("id") or title[:60],
                "title":         title,
                "url":           a.get("link") or a.get("url", ""),
                "body":          a.get("description") or a.get("summary", ""),
                "source":        a.get("source", "free-crypto-news"),
                "published_at":  a.get("pubDate") or a.get("publishedAt", ""),
                "pre_sentiment": sentiment_raw.lower() if sentiment_raw else "",
            })
        logger.info(f"FCN /analyze: {len(items)} artículos")
        return items

    async def _fetch_breaking(self) -> list[dict]:
        """/api/breaking — noticias de las últimas 2 horas."""
        raw = await self._fetch_fcn("/api/breaking", {"limit": 15})
        return self._normalize_fcn(raw, "breaking")

    async def _fetch_trending(self) -> list[dict]:
        """/api/trending — tópicos trending con sentimiento global."""
        raw = await self._fetch_fcn("/api/trending", {"hours": 4})
        # trending devuelve tópicos, no artículos individuales
        items = []
        if isinstance(raw, list):
            for t in raw[:10]:
                title = t.get("topic") or t.get("title") or t.get("keyword", "")
                if not title:
                    continue
                items.append({
                    "id":            f"trend_{title[:40]}",
                    "title":         f"[TRENDING] {title}",
                    "url":           "",
                    "body":          str(t.get("count", "")) + " menciones",
                    "source":        "trending",
                    "published_at":  "",
                    "pre_sentiment": str(t.get("sentiment", "")).lower(),
                })
        logger.info(f"FCN /trending: {len(items)} tópicos")
        return items

    def _normalize_fcn(self, raw: list, source_label: str) -> list[dict]:
        items = []
        for a in raw:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "id":            a.get("link") or title[:60],
                "title":         title,
                "url":           a.get("link") or a.get("url", ""),
                "body":          a.get("description") or "",
                "source":        a.get("source") or source_label,
                "published_at":  a.get("pubDate") or "",
                "pre_sentiment": "",
            })
        return items

    # ─── Espejo GitHub Pages ──────────────────────────────────────────────────

    async def _fetch_failsafe(self) -> list[dict]:
        """Espejo estático en GitHub Pages (cache horario)."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(FCN_FAILSAFE, headers=FCN_HEADERS,
                                 timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
            articles = data.get("articles", []) if isinstance(data, dict) else []
            logger.info(f"FCN failsafe: {len(articles)} artículos")
            return self._normalize_fcn(articles, "cache")
        except Exception as e:
            logger.debug(f"FCN failsafe error: {e}")
            return []

    # ─── RSS último recurso ───────────────────────────────────────────────────

    async def _fetch_rss(self, name: str, url: str) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text(errors="replace")

            items = []
            for i, block in enumerate(re.findall(r"<item[^>]*>(.*?)</item>", text, re.DOTALL)[:12]):
                title_m = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.DOTALL)
                link_m  = re.search(r"<link[^>]*>(?:<!\[CDATA\[)?(https?://.*?)(?:\]\]>)?</link>", block, re.DOTALL)
                desc_m  = re.search(r"<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", block, re.DOTALL)
                title   = title_m.group(1).strip() if title_m else ""
                link    = link_m.group(1).strip()  if link_m  else ""
                desc    = re.sub(r"<[^>]+>", "", desc_m.group(1)).strip()[:200] if desc_m else ""
                if title and len(title) > 10:
                    items.append({
                        "id": link or f"{name}_{i}",
                        "title": title, "url": link, "body": desc,
                        "source": name, "published_at": "", "pre_sentiment": "",
                    })
            logger.info(f"RSS {name}: {len(items)} artículos")
            return items
        except Exception as e:
            logger.debug(f"RSS {name} error: {e}")
            return []

    # ─── Agregador principal ──────────────────────────────────────────────────

    async def fetch_all_news(self) -> list[dict]:
        """
        Lanza todas las fuentes en paralelo y agrega deduplicando por título.
        Si la API principal no devuelve nada, activa el espejo y los RSS.
        """
        # Fuentes principales (paralelo)
        analyzed, breaking = await asyncio.gather(
            self._fetch_analyzed(),
            self._fetch_breaking(),
        )

        all_news = analyzed + breaking

        # Si la API principal falló → failsafe + RSS
        if not all_news:
            logger.warning("API principal sin respuesta — usando failsafe + RSS")
            rss_tasks  = [self._fetch_rss(n, u) for n, u in RSS_SOURCES]
            results    = await asyncio.gather(self._fetch_failsafe(), *rss_tasks)
            for r in results:
                all_news += r

        # Deduplicar por título (primeras 55 chars)
        seen: set = set()
        unique = []
        for item in all_news:
            key = item.get("title", "")[:55].lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(item)

        logger.info(f"Total noticias únicas: {len(unique)}")
        return unique

    # ─── Filtrado y análisis ──────────────────────────────────────────────────

    async def get_important_news(self) -> list[dict]:
        """
        Devuelve las noticias de impacto MEDIUM/HIGH no procesadas aún.
        Si la noticia trae sentimiento ML pre-calculado, lo usa directamente.
        """
        global _processed_news_ids

        raw_news = await self.fetch_all_news()
        important = []

        for item in raw_news[:25]:
            news_id = str(item.get("id") or item.get("url") or item.get("title", ""))[:100]

            if news_id in _processed_news_ids:
                continue

            title = item.get("title", "").strip()
            if not title:
                continue

            # Pre-filtrar por keywords antes de analizar (ahorra tiempo/tokens)
            if not self._is_relevant(title):
                _processed_news_ids.add(news_id)
                continue

            # Analizar (ML pre-score → Claude → reglas según disponibilidad)
            analysis = await self.analyzer.analyze_news(
                title=title,
                content=item.get("body", ""),
                pre_sentiment=item.get("pre_sentiment", ""),
            )

            if analysis.get("impact") in ("HIGH", "MEDIUM"):
                important.append({
                    "id":           news_id,
                    "title":        title,
                    "url":          item.get("url", ""),
                    "source":       item.get("source", ""),
                    "published_at": item.get("published_at",
                                             datetime.now(timezone.utc).isoformat()),
                    **analysis,
                })

            _processed_news_ids.add(news_id)
            if len(_processed_news_ids) > 600:
                _processed_news_ids = set(list(_processed_news_ids)[-400:])

        logger.info(f"Noticias importantes filtradas: {len(important)}")
        return important

    def _is_relevant(self, title: str) -> bool:
        t = title.lower()
        return any(kw in t for kw in HIGH_IMPACT_KEYWORDS)


# ─── Instancias globales ──────────────────────────────────────────────────────
macro_analyzer = MacroAnalyzer()
news_monitor   = NewsMonitor(macro_analyzer)
