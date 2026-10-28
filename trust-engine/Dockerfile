# Trust-engine image: Go service + pinned manglekit binary.
# MANGLEKIT_VERSION must match trust-engine/derive.go ManglekitVersion.
ARG MANGLEKIT_VERSION=v0.0.0-20260330150740-81e7572ffb31

FROM golang:1.25-alpine AS build

WORKDIR /src
COPY trust-engine/go.mod ./
COPY trust-engine/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/trust-engine .

FROM golang:1.25-alpine AS mkitbuild
ARG MANGLEKIT_VERSION
WORKDIR /mkit
RUN go mod init tmpmkit && \
    go mod edit -require=github.com/duynguyendang/manglekit@${MANGLEKIT_VERSION} && \
    GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" \
      -o /out/mkit github.com/duynguyendang/manglekit/cmd/mkit

FROM alpine:3.22

RUN adduser -D -u 10001 app
WORKDIR /app
COPY --from=build /out/trust-engine /usr/local/bin/trust-engine
COPY --from=mkitbuild /out/mkit /usr/local/bin/mkit
COPY trust-engine/policies ./policies
COPY data/mock/taxonomy ./taxonomy-baked
COPY trust-engine/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV MANGLEKIT_BIN=/usr/local/bin/mkit
ENV TAXONOMY_DIR=/app/taxonomy

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
