FROM python:3.12-alpine
WORKDIR /gw
COPY gateway.py .
# Roda como root só para abrir o socket do Docker (dono root, modo 660); sem nenhuma capability.
ENV PYTHONUNBUFFERED=1
EXPOSE 2375
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:2375/health >/dev/null || exit 1
CMD ["python", "-I", "gateway.py"]
