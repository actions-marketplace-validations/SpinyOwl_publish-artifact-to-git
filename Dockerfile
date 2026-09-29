FROM alpine:3

RUN apk add --no-cache rsync git bash coreutils

COPY entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
