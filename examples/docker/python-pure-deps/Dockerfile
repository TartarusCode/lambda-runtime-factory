# A Lambda container image built on a published runtime, with pure-Python
# dependencies installed in a builder stage.
#
# The runtime image is distroless: no shell, no pip, no compiler. Installing
# anything with `RUN` in the final stage cannot work, so dependencies are built
# elsewhere and copied in. Every file the runtime needs (including the
# *.dist-info metadata scanners read) travels in /out.
ARG RUNTIME=ghcr.io/tartaruscode/lambda-runtime-factory/pypy311:3.11

FROM python:3.11-slim AS build
WORKDIR /src
COPY requirements.txt .
# --target keeps the tree self-contained: no interpreter, no site-packages of the
# builder image, just the packages.
RUN pip install --no-cache-dir --target /out -r requirements.txt

FROM ${RUNTIME}
# Lambda mounts the function code at /var/task; WORKDIR already points there.
COPY --from=build /out /var/task
COPY app.py /var/task/
# `CMD` is the handler: the runtime entrypoint reads it (or _HANDLER) and imports
# app.handler. Nothing else has to be configured.
CMD ["app.handler"]
