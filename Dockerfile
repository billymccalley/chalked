FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CHALKED_HOST=0.0.0.0
ENV CHALKED_PORT=8080

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY backend /app/backend

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "backend.chalked_backend.server"]
