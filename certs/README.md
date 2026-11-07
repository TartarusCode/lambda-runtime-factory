# Custom root CAs for the Iron Bank images

Drop PEM-encoded `*.crt` files here and they are baked into every Iron Bank container image's
trust store. `tools/bin/build-container --ironbank` copies `certs/*.crt` into the build context,
and the builder stage of `tools/docker/Dockerfile` runs `update-ca-trust extract`, so the
CAs are trusted by OpenSSL, Python, Node and Deno through the single bundle the image points every
TLS stack at:

```
SSL_CERT_FILE        /etc/pki/tls/certs/ca-bundle.crt
REQUESTS_CA_BUNDLE   /etc/pki/tls/certs/ca-bundle.crt
DENO_CERT            /etc/pki/tls/certs/ca-bundle.crt
NODE_EXTRA_CA_CERTS  /etc/pki/tls/certs/ca-bundle.crt
```

Nothing else is required: an empty directory is valid (the trust-store extraction is skipped when
there is nothing to add), and a certificate added here applies to every runtime on the next build.

## Notes

- Only `*.crt` is picked up; a `.pem` or `.cer` file is ignored rather than silently trusted.
- These are **additional** roots. The base image's own Red Hat/DigiCert roots stay in place.
- This directory is for public certificates only. Never commit a private key.
