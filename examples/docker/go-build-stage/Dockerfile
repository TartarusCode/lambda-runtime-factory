# A Go Lambda container image: compile with the published toolchain image, then
# ship only the binary on an OS-only base.
#
# go126 is a *build* image (debian:12-slim plus the Go toolchain), not a runtime:
# it carries a shell so `RUN go build` works, and it is not meant to be the final
# stage of a function image.
#
# The final stage can be distroless/static because a CGO-free Go binary is static
# and carries the Lambda runtime client (`github.com/aws/aws-lambda-go/lambda`)
# inside it — the platform supplies AWS_LAMBDA_RUNTIME_API and the binary talks to
# it directly, so no runtime layer and no handler string are involved.
ARG TOOLCHAIN=ghcr.io/tartaruscode/lambda-runtime-factory/go126:1.26

FROM ${TOOLCHAIN} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/bootstrap .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/bootstrap /var/task/bootstrap
ENTRYPOINT ["/var/task/bootstrap"]
