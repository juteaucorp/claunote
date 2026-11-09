FROM node:26-slim
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY *.mjs ./
RUN mkdir /data && chown node:node /data
ENV ETAT_FICHIER=/data/compte.json
USER node
CMD ["node", "server.mjs"]
