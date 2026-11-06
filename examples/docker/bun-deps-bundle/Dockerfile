# A Bun Lambda container image with its dependencies bundled at build time.
#
# Bun resolves node_modules at run time, but the runtime image is distroless and
# read-only outside /tmp, so the bundle is produced in a builder stage and copied
# in as plain files.
ARG RUNTIME=ghcr.io/tartaruscode/lambda-runtime-factory/bun13:1.3

FROM oven/bun:1.3-slim AS build
WORKDIR /src
COPY package.json bun.lock* ./
RUN bun install --frozen-lockfile --production
COPY app.ts .
# One file, no node_modules at run time.
RUN bun build app.ts --target bun --outfile /out/app.js

FROM ${RUNTIME}
COPY --from=build /out/app.js /var/task/
# The runtime's entrypoint starts Bun on its helper; this names the module and
# function to import.
CMD ["app.handler"]
