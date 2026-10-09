FROM public.ecr.aws/docker/library/postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
RUN apk add --no-cache age aws-cli bash
COPY archive-wal.sh restore-wal.sh physical-backup.sh restore-physical.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/*-wal.sh /usr/local/bin/*physical*.sh \
    && mkdir -p /var/lib/postgresql/archive && chown postgres:postgres /var/lib/postgresql/archive
