FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN pip install --no-cache-dir Pillow==11.3.0 openpyxl==3.1.5 pypdf==6.0.0 et-xmlfile==2.0.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
WORKDIR /work
CMD ["python", "--version"]
