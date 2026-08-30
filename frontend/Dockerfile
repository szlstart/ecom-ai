# syntax=docker/dockerfile:1.8
FROM node:24.18.0-alpine AS build
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM nginxinc/nginx-unprivileged:1.31.4-alpine AS runtime
ARG BUILD_SHA=development
LABEL org.opencontainers.image.revision="$BUILD_SHA"
USER root
RUN apk upgrade --no-cache
USER 101:101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
