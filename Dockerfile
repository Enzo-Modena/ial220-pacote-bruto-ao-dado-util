FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY analise/ analise/
RUN useradd --create-home analise
USER analise
CMD ["python", "-m", "analise.consultas_mongo"]
