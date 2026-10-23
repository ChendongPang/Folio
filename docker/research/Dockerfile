# Build each target with: docker build --target base -t folio-research-base:v2 -f docker/research/Dockerfile .
# Pin the Python base by digest. OS package security updates are resolved at build time;
# the executor records the resulting immutable image ID for every operation.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
RUN pip install --no-cache-dir openpyxl==3.1.5 python-docx==1.1.2 pypdf==5.6.0 Pillow==11.2.1
RUN useradd --uid 10001 --create-home researcher
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT []
CMD ["python", "--version"]

FROM base AS data
USER root
RUN pip install --no-cache-dir numpy==2.2.6 pandas==2.2.3 scipy==1.15.3
USER 10001:10001

FROM base AS office
USER root
RUN apt-get update && apt-get install --no-install-recommends -y \
    libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils \
    fonts-noto-cjk fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
USER 10001:10001

FROM office AS ocr
USER root
RUN apt-get update && apt-get install --no-install-recommends -y \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
