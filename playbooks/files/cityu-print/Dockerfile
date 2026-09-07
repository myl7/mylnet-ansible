FROM python:3.13-slim

# Ghostscript turns uploaded PDFs into PostScript. The print server passes
# bytes straight to the printer, so the conversion has to happen here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 cityu-print
COPY cityu_print.py /app/cityu_print.py

USER cityu-print
EXPOSE 8631
ENTRYPOINT ["python3", "/app/cityu_print.py"]
