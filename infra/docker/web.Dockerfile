# The dashboard, in two shapes from one file.
#
# `dev` is the Vite dev server, which is what `make up` runs and what the bind
# mounts in docker-compose.yml expect. `prod` is the built bundle served by
# nginx. They are stages rather than two files so that the dependency install
# is written once and cannot drift between them.
#
# The build context is the repository ROOT (as it already is for api and
# worker), not apps/web — the nginx config lives under infra/docker/ with the
# other deployment config, and a context rooted at apps/web cannot reach it.

# ── dev ──────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS dev
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ── build ────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./

# Vite inlines `import.meta.env.VITE_*` into the bundle at BUILD time, so these
# are build args and not runtime environment: setting VITE_API_BASE_URL on the
# running container does nothing at all to a built bundle. That is the trap
# this image exists to close.
#
# Both default to empty, which means "same origin as the page" — the bundle
# then carries no hostname, and one image is correct in every environment. Pass
# them only to point the dashboard at an API somewhere else, which also means
# taking on CORS (`API_CORS_ORIGINS`) and losing that portability.
ARG VITE_API_BASE_URL=""
ARG VITE_WS_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_WS_URL=$VITE_WS_URL

RUN npm run build

# ── prod ─────────────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS prod

# Where the API is, resolved at container start by the nginx image's envsubst
# pass over /etc/nginx/templates. Defaulted here so the template is never left
# holding a literal ${API_UPSTREAM}.
ENV API_UPSTREAM=http://api:8000
# Restrict that substitution pass to exactly this name, so nginx's own
# $host / $request_uri / $http_upgrade are never eaten by a stray env var
# of the same name in the deployment environment.
ENV NGINX_ENVSUBST_FILTER="^API_UPSTREAM$"

COPY --from=build /app/dist /usr/share/nginx/html
COPY infra/docker/web.nginx.conf /etc/nginx/templates/default.conf.template

EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD wget -q -O /dev/null http://localhost/index.html || exit 1
