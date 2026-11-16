# A Go Lambda container image: compile with the published toolchain image, then
# ship only the binary on an OS-only base.
#
# go126 is a *build* image — Iron Bank UBI9-minimal plus the Go toolchain — not a
# runtime: it carries a shell so `RUN go build` works, and it is not meant to be
# the final stage of a function image.
#
# The final stage is yours to choose. `public.ecr.aws/lambda/provided:al2023` is
# the AWS OS-only base for a compiled bootstrap; a CGO-free Go binary is static
# and carries the Lambda runtime client (`github.com/aws/aws-lambda-go/lambda`)
# inside it — the platform supplies AWS_LAMBDA_RUNTIME_API and the binary talks to
# it directly, so no handler string is involved.
ARG TOOLCHAIN=ghcr.io/tartaruscode/lambda-runtime-factory/go126:1.26

FROM ${TOOLCHAIN} AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags='-s -w' -o /out/bootstrap .

FROM public.ecr.aws/lambda/provided:al2023
COPY --from=build /out/bootstrap /var/task/bootstrap
ENTRYPOINT ["/var/task/bootstrap"]
