# Serviço motion: HyperFrames (Apache 2.0) + Chromium + ffmpeg. Versões fixas; roda sem internet (rede interna do compose).
FROM node:22-bookworm-slim

ENV HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1 HYPERFRAMES_SKIP_SKILLS=1 DO_NOT_TRACK=1 \
    PUPPETEER_SKIP_DOWNLOAD=1 HOME=/home/node NODE_ENV=production

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 \
      libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund && chown -R node:node /app
USER node

# Baixa o Chromium e aquece o cache de fontes AGORA (build tem internet; em execução não há).
COPY --chown=node:node warmup.html /tmp/warmup/index.html
RUN node node_modules/hyperframes/dist/cli.js browser ensure \
 && mkdir -p /tmp/warmup/lib && cp node_modules/gsap/dist/gsap.min.js /tmp/warmup/lib/ && echo '{}' > /tmp/warmup/hyperframes.json \
 && cd /tmp/warmup && node /app/node_modules/hyperframes/dist/cli.js render --quality draft -o /tmp/warmup/out.mp4 \
 && rm -rf /tmp/warmup

COPY --chown=node:node server.mjs ./
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.mjs"]
