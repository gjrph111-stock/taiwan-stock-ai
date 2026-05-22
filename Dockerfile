FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY stock_v1 ./stock_v1
COPY data ./data

EXPOSE 8765

CMD ["python", "-m", "stock_v1", "web", "--host", "0.0.0.0"]
