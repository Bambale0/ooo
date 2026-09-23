FROM postgres:16-alpine
RUN apk add --no-cache age aws-cli bash
COPY archive-wal.sh restore-wal.sh physical-backup.sh restore-physical.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/*-wal.sh /usr/local/bin/*physical*.sh \
    && mkdir -p /var/lib/postgresql/archive && chown postgres:postgres /var/lib/postgresql/archive
