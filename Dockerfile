# Multi-stage build: compile the React SPA, then run it from the FastAPI
# backend that serves it (see backend/app/main.py, mounted at /app).
# Render builds this image directly (render.yaml: runtime: docker).

FROM node:22-alpine AS frontend-build
WORKDIR /repo/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /repo/backend

# quickxorhash (Microsoft OneDrive hash verification) has no prebuilt wheel
# for this platform and needs a C compiler to build from source.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /repo/frontend/dist /repo/frontend/dist

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
