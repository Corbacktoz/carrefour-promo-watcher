import os, re, sys, logging
from datetime import datetime
from urllib.parse import urljoin
from urllib import robotparser
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.blocking import BlockingScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === Configuration ===
PROMO_URL = "https://www.carrefour.fr/promotions"
USER_AGENT = "PromoWatcher/1.0 (+contact: you@example.com)"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === Log ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# Regex pour détecter les pourcentages
PCT_RE = re.compile(r"(-?\d{1,3})\s?%")

# === Vérification robots.txt ===
def allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    base = "https://www.carrefour.fr"
    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception as e:
        logging.warning("robots.txt illisible (%s), prudence.", e)
        return True

# === Téléchargement page promotions ===
def fetch_promotions() -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    r = requests.get(PROMO_URL, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text

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
    for card in soup.select("[class*='card'], [class*='Card'], article, li"):
        snippet = " ".join(card.get_text(separator=" ", strip=True).split())
        if PCT_RE.search(snippet):
            cards.append(snippet[:220])
    return sorted(set(hits), reverse=True), cards[:5]

# === Envoi Telegram ===
def send_telegram(msg: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logging.warning("Telegram non configuré.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        logging.error("Envoi Telegram échoué: %s", e)

# === Tâche principale ===
def job_once():
    logging.info("Scan des promotions Carrefour…")
    if not allowed_by_robots(PROMO_URL):
        logging.warning("Accès non autorisé par robots.txt pour %s.", PROMO_URL)
        return
    try:
        html = fetch_promotions()
    except Exception as e:
        logging.error("Erreur téléchargement: %s", e)
        return

    hits, cards = extract_promos(html)
    if hits:
        header = f"🛒 Carrefour : {len(hits)} remise(s) ≥ 50% ({', '.join(str(abs(v))+'%' for v in hits[:5])})"
        body = "\n• " + "\n• ".join(cards) if cards else ""
        msg = f"{header}\n{body}\n\nLien : {PROMO_URL}"
        logging.info("ALERTE envoyée.")
        send_telegram(msg)
    else:
        msg = f"🕓 {datetime.now():%H:%M} – aucune remise ≥ 50% trouvée."
        logging.info(msg)
        send_telegram(msg)

# === Commande /now sur Telegram ===
async def telegram_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Commande Telegram /now reçue.")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="🔎 Vérification manuelle lancée…")
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
