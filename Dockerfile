FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .
RUN mkdir -p data store sessions logs

EXPOSE 8080
CMD ["python", "app.py"]
