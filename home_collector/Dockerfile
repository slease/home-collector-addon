ARG BUILD_FROM
FROM $BUILD_FROM

# Python's standard library only: no pip packages.
RUN apk add --no-cache python3

# The whole folder, so optional job modules beside collector.py (movies_job.py, R-0378) ride along.
COPY . /app/
RUN chmod a+x /app/run.sh

CMD [ "/app/run.sh" ]
