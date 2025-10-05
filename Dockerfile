# Utilise une image avec Playwright préinstallé (Python 3.11)
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Copie les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie le code
COPY promo_watcher_headless.py .

# Les navigateurs sont déjà installés dans l'image de base
# Pas besoin de "playwright install"

# Variables d'environnement par défaut
ENV TZ=Europe/Paris
ENV PYTHONUNBUFFERED=1

# Lance le script
CMD ["python", "promo_watcher_headless.py"]