FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY login_userbot.py ./
COPY config ./config
RUN useradd -m relay && mkdir -p /srv/data && chown -R relay /srv
USER relay
EXPOSE 8080
CMD ["python", "-m", "app.main"]
