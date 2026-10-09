FROM public.ecr.aws/docker/library/postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
RUN apk add --no-cache age aws-cli bash
COPY backup.sh restore-test.sh /usr/local/bin/
USER postgres
ENTRYPOINT ["/usr/local/bin/backup.sh"]
