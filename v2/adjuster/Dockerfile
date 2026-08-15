# Adjustment worker -- Loop 1. Python because the remedies are image work
# (PIL) + a Mistral SDK call + PyMuPDF page surgery, all of which ship
# manylinux wheels. NO apt-get anywhere: the VPS's Cloudflare Gateway blocks
# plain-HTTP apt on port 80 (the root cause of the v2 deploy failures on
# 2026-08-15) and every dependency here arrives as a wheel over 443.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        "psycopg[binary]>=3.1" \
        "pymupdf>=1.24" \
        "boto3>=1.34" \
        "mistralai>=1.2" \
        "pillow>=10.0"

WORKDIR /app
COPY adjuster.py /app/

# psycopg[binary] bundles libpq 18 -> PGSSLNEGOTIATION is honoured. Direct
# TLS-with-SNI is REQUIRED to reach Neon through the Gateway (same fix as v1).
ENV PGSSLNEGOTIATION=direct \
    PYTHONUNBUFFERED=1

CMD ["python", "adjuster.py"]
