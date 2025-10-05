import os, re, sys, logging, time, asyncio
from datetime import datetime
from urllib.parse import urljoin
from urllib import robotparser
from typing import Tuple

from bs4 import BeautifulSoup
import requests

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# === Configuration ===
PROMO_URLS = [
    "https://www.carrefour.fr/promotions",
    "https://www.carrefour.fr/evenements/soldes",
]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SKIP_ROBOTS = os.getenv("SKIP_ROBOTS", "false").lower() == "true"

# === Log ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

PCT_RE = re.compile(r"(-?\d{1,3})\s?%")

# === Vérification robots.txt ===
def allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    if SKIP_ROBOTS:
        logging.warning("SKIP_ROBOTS=true → vérification robots.txt désactivée.")
        return True
    base = "https://www.carrefour.fr"
    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logging.warning("robots.txt illisible (%s). Accès autorisé par défaut.", e)
        return True

# === Extraction des promos ===
def extract_promos(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text_blobs = [t.strip() for t in soup.find_all(string=True) if t.strip()]
    full_text = " \n".join(text_blobs)

    found = []
    for m in PCT_RE.finditer(full_text):
        try:
            val = int(m.group(1).replace("−", "-"))
            found.append(val)
        except ValueError:
            continue

    hits = [v for v in found if abs(v) >= 50]

    cards = []
    for card in soup.select("[class*='card'], [class*='Card'], article, li, [data-testid*='card']"):
        snippet = " ".join(card.get_text(separator=" ", strip=True).split())
        if PCT_RE.search(snippet):
            cards.append(snippet[:220])

    return sorted(set(hits), reverse=True), cards[:5]

# === Envoi Telegram (async) ===
async def send_telegram(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logging.warning("Telegram non configuré.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: requests.post(url, json=payload, timeout=15))
    except Exception as e:
        logging.error("Envoi Telegram échoué: %s", e)

# === Fetch avec Playwright (async) ===
async def fetch_with_playwright(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=[
                '--no-sandbox', 
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ]
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            viewport={'width': 1920, 'height': 1080},
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        
        # Masquer les traces de Playwright
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
        """)
        
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Gestion cookies
            consent_selectors = [
                "button:has-text('Accepter')",
                "button:has-text('Tout accepter')",
                "button:has-text('J'accepte')",
            ]
            for sel in consent_selectors:
                try:
                    await page.locator(sel).first.click(timeout=2000)
                    logging.info("Bannière cookies fermée (%s).", sel)
                    break
                except PWTimeout:
                    pass

            # Fermeture modals
            close_selectors = [
                "button[aria-label='Fermer']",
                "button:has-text('Fermer')",
            ]
            for sel in close_selectors:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    logging.info("Modal fermé (%s).", sel)
                    break
                except PWTimeout:
                    pass

            await page.wait_for_load_state("networkidle", timeout=20000)
            await asyncio.sleep(2.0)  # Temps supplémentaire pour chargement dynamique

            html = await page.content()
            return html
        finally:
            await ctx.close()
            await browser.close()

# === Fetch fallback (requests) ===
def fetch_requests(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return r.text

# === Essaie toutes les URLs ===
async def fetch_first_ok() -> Tuple[str, str]:
    last_err = None
    for url in PROMO_URLS:
        if not allowed_by_robots(url):
            logging.warning("Accès refusé par robots.txt pour %s.", url)
            continue
        
        # Tentative Playwright
        try:
            html = await fetch_with_playwright(url)
            logging.info("✓ Fetch OK (Playwright) sur %s", url)
            return url, html
        except Exception as e:
            logging.warning("✗ Playwright KO sur %s : %s", url, e)
            last_err = e
        
        # Fallback requests
        try:
            loop = asyncio.get_event_loop()
            html = await loop.run_in_executor(None, fetch_requests, url)
            logging.info("✓ Fetch OK (requests) sur %s", url)
            return url, html
        except Exception as e:
            logging.warning("✗ Requests KO sur %s : %s", url, e)
            last_err = e
    
    raise last_err or RuntimeError("Toutes les URL ont échoué")

# === Tâche principale (async) ===
async def job_once():
    logging.info("🔍 Scan des promotions Carrefour…")
    try:
        used_url, html = await fetch_first_ok()
    except Exception as e:
        logging.error("❌ Erreur téléchargement: %s", e)
        await send_telegram(f"⚠️ Erreur de téléchargement des pages promos: {e}")
        return

    hits, cards = extract_promos(html)
    if hits:
        header = f"🛒 Carrefour : {len(hits)} remise(s) ≥ 50% ({', '.join(str(abs(v))+'%' for v in hits[:5])})"
        body = "\n• " + "\n• ".join(cards) if cards else ""
        msg = f"{header}\n{body}\n\nSource : {used_url}"
        logging.info("✅ ALERTE envoyée.")
        await send_telegram(msg)
    else:
        msg = f"🕐 {datetime.now():%H:%M} — aucune remise ≥ 50% trouvée (source : {used_url})."
        logging.info(msg)
        await send_telegram(msg)

# === Commande Telegram /now ===
async def telegram_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("📱 Commande /now reçue.")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🔎 Vérification en cours…")
    await job_once()

# === Main avec scheduler + Telegram ===
async def main_async():
    # Configuration du scheduler
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(job_once, "cron", hour=9, minute=5, id="daily_check")
    scheduler.start()
    logging.info("⏰ Planifié chaque jour à 09:05 Europe/Paris.")

    if TELEGRAM_BOT_TOKEN:
        # Configuration du bot Telegram
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("now", telegram_now))
        
        # Initialise le bot et commence le polling
        await app.initialize()
        await app.start()
        logging.info("🤖 Bot Telegram démarré avec commande /now")
        
        # Garde le programme en vie
        try:
            await app.updater.start_polling()
            # Attendre indéfiniment
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
    else:
        logging.warning("⚠️ Pas de token Telegram -> mode console seul.")
        # Garde le scheduler en vie
        await asyncio.Event().wait()

def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "now":
        asyncio.run(job_once())
        return

    # Lance la boucle principale
    asyncio.run(main_async())

if __name__ == "__main__":
    main()