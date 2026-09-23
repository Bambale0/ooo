FROM postgres:16-alpine
RUN apk add --no-cache age aws-cli bash
COPY backup.sh restore-test.sh /usr/local/bin/
USER postgres
ENTRYPOINT ["/usr/local/bin/backup.sh"]
