ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

# Copy data for add-on
COPY src/ /

# Install requirements for add-on
RUN apk add --no-cache -U python3 py3-paho-mqtt py3-jsonschema py3-zeroconf

# vantage2mqtt stores device cache data in current working directory
# So make workdir persistent data directory
WORKDIR /data

RUN chmod a+x /vantage2mqtt.py

CMD [ "/vantage2mqtt.py", "--config", "options.json" ]
