# OCR review app -- deployable image for Coolify.
#
# The app holds NO document data. Text, confidence, corrections and verdicts all
# live in Neon; page images live in R2 and are fetched by the browser directly
# from a signed URL. So this image stays small and carries nothing sensitive,
# and the container can be rebuilt or moved without touching the corpus.
FROM python:3.12-slim

# PyMuPDF ships manylinux wheels, so no build toolchain is needed. It is kept
# in the image because it powers the local-render fallback when R2 is not
# configured -- the same file then runs unchanged on Alden's laptop.
RUN pip install --no-cache-dir \
        "psycopg[binary]>=3.1" \
        "pymupdf>=1.24" \
        "boto3>=1.34"

WORKDIR /app
COPY ocr_review_app.py /app/

# Coolify's proxy terminates TLS and forwards here, so bind all interfaces.
ENV HOST=0.0.0.0 \
    PORT=8778 \
    NO_BROWSER=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8778

# Fails the container if the app cannot answer, so Coolify restarts it rather
# than leaving a dead page in front of a reviewer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8778/healthz',timeout=4).status==200 else 1)"

CMD ["python", "ocr_review_app.py"]
