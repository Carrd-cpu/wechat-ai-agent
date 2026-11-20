FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY . .

EXPOSE 8000
CMD ["python", "server.py", "--no-browser"]
