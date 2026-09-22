# ARGUS frontend — production image.
#
#     docker build -f frontend/Dockerfile -t argus-frontend frontend
#
# Unlike the backend, this builds from `frontend/` — it needs nothing outside it.
#
# Three stages. The point of the third is that Next's `standalone` output traces
# exactly which modules the app imports, so the runtime image carries those
# instead of all of node_modules.

# --- dependencies -----------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
# `npm ci` from the lockfile only, so this layer is cached until a dependency
# actually changes rather than on every source edit.
COPY package.json package-lock.json ./
RUN npm ci

# --- build ------------------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Baked in at build time, not read at runtime: `NEXT_PUBLIC_*` values are
# inlined into the client bundle by the compiler, so this has to be the URL the
# *browser* will use — not an internal service name the browser cannot resolve.
# It is therefore a build argument, and a different deployment target needs a
# different image.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The node image ships a `node` user; reusing it avoids inventing another uid.
RUN apk add --no-cache curl

# `standalone` contains its own minimal server and the traced modules. `static`
# and `public` are not included in it and have to be copied alongside, at the
# paths the server expects.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:3000/ || exit 1

# The standalone server, not `next start`: the Next CLI is not in this image.
CMD ["node", "server.js"]
