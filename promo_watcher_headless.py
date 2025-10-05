import os, re, sys, logging, time
from datetime import datetime
from urllib.parse import urljoin
from urllib import robotparser

from bs4 import BeautifulSoup
import requests

from apscheduler.schedulers.blocking import BlockingScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# === Configuration ===
PROMO_URLS = [
    "https://www.carrefour.fr/promotions",         # cible principale (nécessite JS/cookies)
    "https://www.carrefour.fr/evenements/soldes",  # fallback public
]
USER_AGENT = "PromoWatcher/1.0 (+contact: you@example.com)"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SKIP_ROBOTS = os.getenv("SKIP_ROBOTS", "false").lower() == "true"

# === Log ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# Regex pour détecter les pourcentages
PCT_RE = re.compile(r"(-?\d{1,3})\s?%")

# === Vérification robots.txt (option SKIP_ROBOTS) ===
def allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    if SKIP_ROBOTS:
        logging.warning("SKIP_ROBOTS=true → vérification robots.txt désactivée (mode test).")
        return True
    base = "https://www.carrefour.fr"
    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logging.warning("robots.txt illisible (%s). On considère l'accès autorisé (mode permissif).", e)
        return True

# === Extraction des promos depuis HTML ===
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
    for card in soup.select("[class*='card'], [class*='Card'], article, li, [data-testid*='card'], [data-test*='card']"):
        snippet = " ".join(card.get_text(separator=" ", strip=True).split())
        if PCT_RE.search(snippet):
            cards.append(snippet[:220])

    return sorted(set(hits), reverse=True), cards[:5]

# === Envoi Telegram ===
def send_telegram(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logging.warning("Telegram non configuré (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID manquants).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logging.error("Envoi Telegram échoué: %s", e)

# === Fetch via Playwright (gère cookies/JS) ===
def fetch_with_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9"},
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Bannières cookies / consentement : on tente quelques sélecteurs “classiques”
            consent_selectors = [
                "button:has-text('Accepter')",
                "button:has-text('Tout accepter')",
                "button:has-text('J’accepte')",
                "button:has-text('J accepte')",
                "button:has-text('Continuer sans accepter')",
                "[aria-label*='accepter']",
            ]
            for sel in consent_selectors:
                try:
                    page.locator(sel).first.click(timeout=2000)
                    logging.info("Bannière cookies: bouton cliqué (%s).", sel)
                    break
                except PWTimeout:
                    pass
                except Exception:
                    pass

            # Parfois sélection de magasin : on essaie de fermer un éventuel modal
            close_selectors = [
                "button[aria-label='Fermer']",
                "button:has-text('Fermer')",
                "button[aria-label*='Close']",
            ]
            for sel in close_selectors:
                try:
                    page.locator(sel).first.click(timeout=1500)
                    logging.info("Modal fermé (%s).", sel)
                    break
                except PWTimeout:
                    pass
                except Exception:
                    pass

            # Laisser charger les blocs promos
            page.wait_for_load_state("networkidle", timeout=20000)
            # petite marge
            time.sleep(1.0)

            html = page.content()
            return html
        finally:
            ctx.close()
            browser.close()

# === Fetch fallback classique (requests) ===
def fetch_requests(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    return r.text

# === Essaie les URL l’une après l’autre, d’abord Playwright, puis fallback requests ===
def fetch_first_ok() -> tuple[str, str]:
    last_err = None
    for url in PROMO_URLS:
        # robots check (pour la 1ère surtout)
        if not allowed_by_robots(url):
            logging.warning("Accès refusé par robots.txt pour %s.", url)
            continue
        # 1) tentative headless
        try:
            html = fetch_with_playwright(url)
            logging.info("Fetch OK (Playwright) sur %s", url)
            return url, html
        except Exception as e:
            logging.warning("Playwright KO sur %s : %s", url, e)
            last_err = e
        # 2) fallback simple requests (utile sur les pages “HTML classiques”)
        try:
            html = fetch_requests(url)
            logging.info("Fetch OK (requests) sur %s", url)
            return url, html
        except Exception as e:
            logging.warning("Requests KO sur %s : %s", url, e)
            last_err = e
    raise last_err or RuntimeError("Toutes les URL ont échoué")

# === Tâche principale ===
def job_once():
    logging.info("Scan des promotions Carrefour…")
    try:
        used_url, html = fetch_first_ok()
    except Exception as e:
        logging.error("Erreur téléchargement (toutes URL): %s", e)
        send_telegram(f"⚠️ Erreur de téléchargement des pages promos: {e}")
        return

    hits, cards = extract_promos(html)
    if hits:
        header = f"🛒 Carrefour : {len(hits)} remise(s) ≥ 50% ({', '.join(str(abs(v))+'%' for v in hits[:5])})"
        body = "\n• " + "\n• ".join(cards) if cards else ""
        msg = f"{header}\n{body}\n\nSource : {used_url}"
        logging.info("ALERTE envoyée.")
        send_telegram(msg)
    else:
        msg = f"🕓 {datetime.now():%H:%M} – aucune remise ≥ 50% trouvée (source : {used_url})."
        logging.info(msg)
        send_telegram(msg)

# === Commande /now sur Telegram ===
async def telegram_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Commande Telegram /now reçue.")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🔎 Vérification manuelle (headless) en cours…")
    job_once()

# === Boucle principale ===
def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "now":
        job_once()
        return

    scheduler = BlockingScheduler(timezone="Europe/Paris")
    scheduler.add_job(job_once, "cron", hour=9, minute=5, id="daily_check")
    logging.info("Planifié chaque jour à 09:05 Europe/Paris.")

    if TELEGRAM_BOT_TOKEN:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app.add_handler(CommandHandler("now", telegram_now))
        logging.info("Commande Telegram /now disponible.")
        app.run_polling()
    else:
        logging.warning("Aucun token Telegram -> mode console seul.")
        scheduler.start()

if __name__ == "__main__":
    main()
