FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY frontend/package.json ./frontend/package.json
COPY server/package.json ./server/package.json
COPY packages/contracts/package.json ./packages/contracts/package.json
RUN npm ci
COPY packages/contracts/ ./packages/contracts/
COPY server/ ./server/
COPY frontend/ ./frontend/
COPY web/static/fonts/ ./web/static/fonts/
COPY web/static/favicon.ico web/static/apple-icon.png ./web/static/
RUN npm run build

FROM build AS test
CMD ["npm", "test"]

FROM build AS dependencies
RUN npm prune --omit=dev

FROM node:24-bookworm-slim AS runtime
WORKDIR /app
ENV APP_ENV=production
ENV HOST=0.0.0.0
ENV PORT=8000
COPY docker-compose.prod.yml ./docker-compose.prod.yml
COPY --from=dependencies /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/server/package.json ./server/package.json
COPY --from=build /app/server/dist ./server/dist
COPY --from=build /app/server/data ./server/data
COPY --from=build /app/server/migrations ./server/migrations
COPY --from=build /app/packages/contracts/package.json ./packages/contracts/package.json
COPY --from=build /app/packages/contracts/dist ./packages/contracts/dist
COPY --from=build /app/web/static/app ./web/static/app
COPY --from=build /app/web/static/fonts ./web/static/fonts
COPY --from=build /app/web/static/favicon.ico /app/web/static/apple-icon.png ./web/static/
CMD ["node", "server/dist/index.js"]
