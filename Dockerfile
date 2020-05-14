ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8

# Copy data for add-on
COPY src/ /
COPY requirements.txt /

# Install requirements for add-on
RUN apk add --no-cache -U python3 && pip3 install --no-cache --upgrade pip
RUN pip3 install \
        --no-cache-dir \
        --prefer-binary \
        -r /requirements.txt

RUN rm /requirements.txt

# vantage2mqtt stores device cache data in current working directory
# So make workdir persistent data directory
WORKDIR /data
RUN chmod a+x /vantage2mqtt.py

CMD [ "/vantage2mqtt.py", "--config", "options.json" ]
