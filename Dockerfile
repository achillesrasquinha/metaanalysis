

FROM  python:3.7-alpine

LABEL maintainer=achillesrasquinha@gmail.com

ENV GEOMEAT_PATH=/usr/local/src/geomeat

RUN apk add --no-cache \
        bash \
        git \
    && mkdir -p $GEOMEAT_PATH

COPY . $GEOMEAT_PATH
COPY ./docker/entrypoint.sh /entrypoint.sh

WORKDIR $GEOMEAT_PATH

RUN pip install -r ./requirements.txt && \
    python setup.py install

ENTRYPOINT ["/entrypoint.sh"]

CMD ["geomeat"]